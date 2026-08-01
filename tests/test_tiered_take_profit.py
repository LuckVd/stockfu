from collections import deque

import pytest

from stockfu.ai.action import _total_to_weight, compute_target_weight
from stockfu.backtest.engine import (
    _apply_portfolio_brake,
    _block_portfolio_new_buys,
    _update_atr_percent,
    atr_take_profit_action,
    tiered_take_profit_action,
    tiered_take_profit_reason,
)


def test_selective_portfolio_brake_blocks_buys_but_allows_reductions():
    current = {"existing": 0.04, "reduce": 0.05, "new": 0.0, "hold": 0.03}
    final = {"existing": 0.05, "reduce": 0.03, "new": 0.02, "hold": None}
    blocked = _block_portfolio_new_buys(final, current)
    assert blocked == {"existing": 0.04, "reduce": 0.03, "new": 0.0, "hold": None}


def test_portfolio_brake_smooth_scales_positive_and_keeps_maintain_cap():
    # 平滑刹车:正目标 ×scale;维持(None)落为 current;总敞口压到 brake_max_gross。
    current = {"a": 0.04, "b": 0.05, "c": 0.03, "d": 0.0}
    final = {"a": 0.05, "b": None, "c": 0.04, "d": 0.02}
    out = _apply_portfolio_brake(
        final, current, {},
        scale=0.75, mode="scale_all", brake_max_gross=0.6,
    )
    # a: 0.05*0.75=0.0375; c: 0.04*0.75=0.03; d: 0.02*0.75=0.015; b 维持=current 0.05
    # 合计 0.0375+0.05+0.03+0.015=0.1325 <= 0.6 → 不再缩放
    assert out["a"] == pytest.approx(0.0375)
    assert out["b"] == 0.05
    assert out["c"] == pytest.approx(0.03)
    assert out["d"] == pytest.approx(0.015)


def test_portfolio_brake_max_gross_caps_maintain_positions():
    # 维持仓占大头时,组合级 cap 必须把总敞口压到 brake_max_gross(2008 根因修复)。
    current = {"a": 0.30, "b": 0.30, "c": 0.30, "d": 0.0}
    final = {"a": None, "b": None, "c": None, "d": 0.05}
    out = _apply_portfolio_brake(
        final, current, {},
        scale=0.75, mode="scale_all", brake_max_gross=0.6,
    )
    gross = sum(w for w in out.values() if w)
    assert gross <= 0.6 + 1e-9
    # 维持仓显式化后参与 cap:等比缩到 0.6
    assert out["a"] < 0.30 and out["a"] > 0.15
    assert out["b"] == out["c"]  # 等比缩放一致


def test_portfolio_brake_keep_ratio_drops_low_score():
    current = {"high": 0.04, "mid": 0.04, "low": 0.04}
    final = {"high": 0.05, "mid": 0.05, "low": 0.05}
    meta = {"high": {"raw": 20.0}, "mid": {"raw": 8.0}, "low": {"raw": -5.0}}
    out = _apply_portfolio_brake(
        final, current, meta,
        scale=0.75, mode="scale_all", brake_max_gross=None,
        keep_ratio=0.5,
    )
    # 3 个正目标 × keep_ratio 0.5 → 保留 top 1(高分);其余清 0。
    assert out["low"] == 0.0          # 低分被清
    assert out["mid"] == 0.0          # 未入保留集也被清
    assert out["high"] == pytest.approx(0.0375)


def test_portfolio_brake_drawdown_add_gated_by_min_score():
    current = {"strong": 0.02, "weak": 0.02}
    final = {"strong": 0.05, "weak": 0.05}
    meta = {"strong": {"raw": 15.0}, "weak": {"raw": 5.0}}
    out = _apply_portfolio_brake(
        final, current, meta,
        scale=1.20, mode="scale_all", brake_max_gross=None,
        add_min_score=12.0, max_weight=0.05,
    )
    assert out["strong"] == pytest.approx(0.05)   # 0.05*1.2 封顶 0.05
    assert out["weak"] == 0.05                      # 未过门控不放大


def test_portfolio_brake_tiers_scale_gt1_gating_caps_gross():
    # 融合语义:浅回调(近满仓)放大 strong_buy,同时 tiers 档位兜底总敞口上限。
    # 放大仅对未达单股上限(w<max_weight)的目标生效(engine._apply_portfolio_brake)。
    current = {}
    final = {f"s{i}": 0.03 for i in range(20)}
    final.update({f"w{i}": 0.03 for i in range(10)})
    meta = {**{f"s{i}": {"raw": 15.0} for i in range(20)},
            **{f"w{i}": {"raw": 5.0} for i in range(10)}}
    out = _apply_portfolio_brake(
        final, current, meta,
        scale=1.20, mode="scale_all", brake_max_gross=0.65,
        add_min_score=12.0, max_weight=0.05,
    )
    # strong_buy(20只) ×1.2 → 0.036;weak(10只) 不放大 → 0.03;
    # 合计 20*0.036+10*0.03=1.02 > 0.65 → 等比缩到 tier 档位 0.65。
    gross = sum(w for w in out.values() if w)
    assert gross == pytest.approx(0.65)
    factor = 0.65 / (20 * 0.036 + 10 * 0.03)
    assert out["s0"] == pytest.approx(0.036 * factor)  # 放大后参与 cap
    assert out["w0"] == pytest.approx(0.03 * factor)   # 未放大也参与 cap
    assert out["s0"] > out["w0"]                        # 加仓倾斜保留



def test_portfolio_brake_block_mode_delegates():
    current = {"existing": 0.04, "new": 0.0}
    final = {"existing": 0.05, "new": 0.02}
    out = _apply_portfolio_brake(
        final, current, {},
        scale=0.75, mode="block_new_buys", brake_max_gross=None,
    )
    assert out == {"existing": 0.04, "new": 0.0}


# ---------------------------------------------------------------------------
# 买卖不对称滞回(双总分):空仓用 buy 分判定建仓,持仓用 sell 分判定清仓。
# ---------------------------------------------------------------------------


def test_total_to_weight_legacy_symmetric_deadzone_unchanged():
    # 旧路径(未传 total_sell):对称死区,行为与历史一致。
    assert _total_to_weight(-5, max_w=0.05, dead=3, score_full=8) == 0.0
    assert _total_to_weight(2, max_w=0.05, dead=3, score_full=8) is None
    assert _total_to_weight(4, max_w=0.05, dead=3, score_full=8) == pytest.approx(0.05 * 4 / 8)


def test_total_to_weight_empty_position_uses_buy_score_only():
    # 空仓:买入分 < dead 不建仓;≥ dead 线性建仓;满分满仓。
    assert _total_to_weight(3, total_sell=-2, held=False, max_w=0.05, dead=5, score_full=8) is None
    assert _total_to_weight(6, total_sell=10, held=False, max_w=0.05, dead=5, score_full=8) == pytest.approx(0.05 * 6 / 8)
    assert _total_to_weight(20, total_sell=-50, held=False, max_w=0.05, dead=5, score_full=8) == pytest.approx(0.05)


def test_total_to_weight_holding_keeps_on_buy_dip_but_sell_breaks():
    # 持仓:卖出分未破 -dead 时,买入分小降不清仓(滞回);跌破 -dead 才清仓。
    # 买入分 3(买入线 5 之下)但卖出分 -4(清仓线 -5 之上)→ 维持。
    assert _total_to_weight(3, total_sell=-4, held=True, max_w=0.05, dead=5, score_full=8) is None
    # 买入分 3 且卖出分 -6 → 清仓。
    assert _total_to_weight(3, total_sell=-6, held=True, max_w=0.05, dead=5, score_full=8) == 0.0
    # 买入分回升到 6 → 持仓可继续加仓。
    assert _total_to_weight(6, total_sell=-4, held=True, max_w=0.05, dead=5, score_full=8) == pytest.approx(0.05 * 6 / 8)


def test_compute_target_weight_hysteresis_full_chain():
    # compute_target_weight 透传 current_weight>0 推导 held。
    # 持仓中(0.04),买入分 3、卖出分 -4 → 维持(不清仓)。
    assert compute_target_weight(False, 0.04, total_score=3, total_sell_score=-4,
                                 max_w=0.05, dead=5, score_full=8) is None
    # 持仓中(0.04),卖出分 -6 → 清仓。
    assert compute_target_weight(False, 0.04, total_score=3, total_sell_score=-6,
                                 max_w=0.05, dead=5, score_full=8) == 0.0
    # 空仓(0.0),买入分 3 → 不建仓。
    assert compute_target_weight(False, 0.0, total_score=3, total_sell_score=-6,
                                 max_w=0.05, dead=5, score_full=8) is None
    # 空仓,买入分 6 → 建仓。
    assert compute_target_weight(False, 0.0, total_score=6, total_sell_score=20,
                                 max_w=0.05, dead=5, score_full=8) == pytest.approx(0.05 * 6 / 8)
    # risk 一票否决优先于双总分滞回。
    assert compute_target_weight(True, 0.04, total_score=6, total_sell_score=-2,
                                 max_w=0.05, dead=5, score_full=8) == 0.0


def test_total_to_weight_legacy_kept_for_no_sell_score():
    # 旧调用方不传 total_sell_score 时,行为与历史一致(回归)。
    assert compute_target_weight(False, 0.04, total_score=-5, max_w=0.05, dead=3, score_full=8) == 0.0
    assert compute_target_weight(False, 0.04, total_score=2, max_w=0.05, dead=3, score_full=8) is None
    assert compute_target_weight(False, 0.0, total_score=4, max_w=0.05, dead=3, score_full=8) == pytest.approx(0.05 * 4 / 8)



def test_take_profit_is_disabled_without_config():
    assert tiered_take_profit_reason(10, 13, 12) is None


def test_take_profit_trailing_tiers_use_highest_reached_tier():
    tiers = ((0.20, 0.05), (0.30, 0.03))
    assert tiered_take_profit_reason(10, 12, 11.4, tiers) == "take_profit_trailing_0.2_0.05"
    assert tiered_take_profit_reason(10, 13, 12.6, tiers) == "take_profit_trailing_0.3_0.03"


def test_take_profit_hard_threshold_has_priority():
    tiers = ((0.20, 0.05), (0.30, 0.03))
    assert tiered_take_profit_reason(10, 15, 15, tiers, 0.50) == "take_profit_hard_0.5"


def test_partial_take_profit_fires_each_tier_once():
    tiers = ((0.20, 0.05, 1 / 3), (0.30, 0.03, 1 / 3))
    first = tiered_take_profit_action(10, 12, 11.4, tiers)
    assert first == ("take_profit_trailing_0.2_0.05", 1 / 3, "take_profit_trailing_0.2_0.05")

    fired = {first[2]}
    assert tiered_take_profit_action(10, 12, 11.4, tiers, fired_tiers=fired) is None
    second = tiered_take_profit_action(10, 13, 12.6, tiers, fired_tiers=fired)
    assert second == ("take_profit_trailing_0.3_0.03", 1 / 3, "take_profit_trailing_0.3_0.03")


def test_atr_percent_uses_true_range_and_warms_up():
    history = deque(maxlen=2)
    previous, atr = _update_atr_percent(
        {"high": 11.0, "low": 9.0, "close": 10.0}, None, history, 2,
    )
    assert previous == 10.0
    assert atr is None
    previous, atr = _update_atr_percent(
        {"high": 10.5, "low": 9.5, "close": 10.0}, previous, history, 2,
    )
    assert previous == 10.0
    assert atr == pytest.approx(0.15)


def test_atr_take_profit_uses_stable_stage_ids_and_partial_fractions():
    tiers = ((0.20, 2.0, 1 / 3), (0.30, 1.25, 1 / 3))
    first = atr_take_profit_action(10, 12, 11.4, 0.025, tiers)
    assert first == ("take_profit_atr_0.2_2", 1 / 3, "take_profit_atr_0.2_2")

    fired = {first[2]}
    assert atr_take_profit_action(10, 12, 11.4, 0.02, tiers, fired_tiers=fired) is None
    second = atr_take_profit_action(10, 13, 12.6, 0.02, tiers, fired_tiers=fired)
    assert second == ("take_profit_atr_0.3_1.25", 1 / 3, "take_profit_atr_0.3_1.25")
