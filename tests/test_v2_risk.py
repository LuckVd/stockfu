"""V2 风险层回归：V1 语义、状态恢复与独立因子量纲。"""
from __future__ import annotations

from datetime import date

import pytest

from stockfu.backtest.engine import Position, VirtualAccount
from stockfu.backtest.v2_run import build_v2_config
from stockfu.scoring.contracts import RawFactorObservation
from stockfu.scoring.history import HistoryState
from stockfu.scoring.profiles import profile_from_dict
from stockfu.scoring.scorer import FactorScorer
from stockfu.strategy.alpha import AlphaAggregator, AlphaDefinition, AlphaFactor
from stockfu.strategy.risk import RiskOverlay, RiskPolicy, TakeProfitTier


def _account(price: float = 100.0) -> VirtualAccount:
    account = VirtualAccount(100_000.0)
    account.cash = 0.0
    account.positions["A"] = Position(
        shares=1000,
        avg_cost=100.0,
        lots=[(1000, date(2023, 1, 3))],
        peak_close=price,
        take_profit_anchor_shares=1000,
    )
    return account


def test_v1_stop_loss_and_drawdown_brake_change_target_only():
    account = _account()
    stop = RiskOverlay(RiskPolicy(risk_policy_id="sl", version=1, stop_loss=0.10))
    assert stop.apply({"A": 0.8}, account, {"A": 90.0}, date(2024, 1, 2),
                      execution_prices={"A": 90.0})["A"] == 0.0
    assert stop.metrics()["risk_stop_loss_count"] == 1

    account = _account()
    brake = RiskOverlay(RiskPolicy(
        risk_policy_id="brake", version=1, drawdown_brake=0.10,
        drawdown_brake_scale=0.50,
    ))
    brake.apply({"A": 0.8}, account, {"A": 100.0}, date(2024, 1, 2),
                execution_prices={"A": 100.0})
    out = brake.apply({"A": 0.8}, account, {"A": 85.0}, date(2024, 1, 3),
                      execution_prices={"A": 85.0})
    assert out["A"] == pytest.approx(0.4)
    assert brake.metrics()["risk_drawdown_brake_count"] == 1


def test_risk_overlay_reuses_ideal_target_without_compounding_scale():
    """连续风险日应稳定压到理想目标的 50%，不能在已压目标上再次乘 50%。"""
    account = _account()
    risk = RiskOverlay(RiskPolicy(
        risk_policy_id="brake_stable", version=1,
        drawdown_brake=0.10, drawdown_brake_scale=0.50,
    ))
    ideal = {"A": 0.8}
    risk.apply(ideal, account, {"A": 100.0}, date(2024, 1, 2),
               execution_prices={"A": 100.0})
    first = risk.apply(ideal, account, {"A": 85.0}, date(2024, 1, 3),
                       execution_prices={"A": 85.0})
    second = risk.apply(ideal, account, {"A": 85.0}, date(2024, 1, 4),
                        execution_prices={"A": 85.0})
    assert first["A"] == pytest.approx(0.4)
    assert second["A"] == pytest.approx(0.4)
    assert risk.last_adjusted is True


def test_v1_partial_take_profit_uses_lots_and_is_checkpointable():
    policy = RiskPolicy(
        risk_policy_id="partial", version=1,
        take_profit_tiers=(
            TakeProfitTier(0.20, 0.05, 1 / 3),
            TakeProfitTier(0.30, 0.03, 1 / 3),
        ),
    )
    risk = RiskOverlay(policy)
    account = _account()
    risk.apply({"A": 0.8}, account, {"A": 120.0}, date(2024, 1, 2),
               execution_prices={"A": 120.0})
    out = risk.apply({"A": 0.8}, account, {"A": 114.0}, date(2024, 1, 3),
                     execution_prices={"A": 114.0})
    assert account.positions["A"].take_profit_fired == {
        "take_profit_trailing_0.2_0.05"
    }
    assert account.positions["A"].take_profit_cap_shares == 600
    assert out["A"] == pytest.approx(0.6)
    assert "A" in risk.forced_exit_codes

    state = risk.checkpoint_state()
    restored = RiskOverlay(policy)
    restored.restore_state(state)
    assert restored.checkpoint_state() == state


def test_market_regime_cap_and_policy_config():
    risk = RiskOverlay(RiskPolicy(
        risk_policy_id="trend", version=1,
        market_regime_code="sh000300", market_regime_ma_days=5,
        market_regime_max_gross=0.50,
    ))
    account = _account()
    out = risk.apply(
        {"A": 0.8}, account, {"A": 100.0}, date(2024, 1, 2),
        execution_prices={"A": 100.0},
        benchmark_closes=[100.0, 102.0, 101.0, 90.0, 89.0],
    )
    assert out["A"] == pytest.approx(0.50)
    assert risk.metrics()["risk_market_regime_count"] == 1

    cfg = build_v2_config(
        "dividend_low_vol_v2", "cn_equity_top15_v2", "v1_core_v1",
        ["600519"], date(2021, 1, 1), date(2021, 2, 1), date(2018, 1, 1),
        observation_count=1,
    )
    assert cfg.risk.policy.stop_loss == pytest.approx(0.30)
    assert cfg.risk.policy.market_regime_ma_days == 200


def test_factor_profiles_keep_independent_raw_units_and_bounds():
    def profile(pid, metric, unit, knots):
        return profile_from_dict({
            "profile_id": pid, "version": 1,
            "raw_metric": {"id": metric, "params": {}},
            "direction": "higher_is_better", "raw_unit": unit,
            "mapping": {"mode": "hybrid", "components": {
                "absolute": {"weight": 1.0, "knots": knots},
            }},
        })

    percent = profile("percent_v1", "yield_percent", "percent",
                      [[0.0, 0.0], [10.0, 100.0]])
    ratio = profile("ratio_v1", "margin_ratio", "ratio",
                    [[0.0, 0.0], [1.0, 100.0]])
    history = HistoryState()
    as_of = date(2024, 1, 3)

    def raw(metric, value, unit):
        return RawFactorObservation(
            asset_code="A", as_of=as_of, raw_metric_id=metric,
            raw_value=value, raw_unit=unit, source_max_date=as_of,
            available_at=as_of, valid=True, raw_fingerprint=metric,
        )

    p_score = FactorScorer(percent).score(
        raw("yield_percent", 5.0, "percent"), history, None, "cn", None)
    r_score = FactorScorer(ratio).score(
        raw("margin_ratio", 0.5, "ratio"), history, None, "cn", None)
    assert p_score.score == pytest.approx(50.0)
    assert r_score.score == pytest.approx(50.0)

    alpha = AlphaDefinition(
        alpha_id="mixed_units", version=1, market_scope="cn",
        factors=(AlphaFactor("percent_v1", 1.0, True),
                 AlphaFactor("ratio_v1", 1.0, True)),
        minimum_coverage=1.0, minimum_valid_factor_count=2,
    )
    combined = AlphaAggregator(alpha).aggregate(
        "A", as_of, {"percent_v1": p_score, "ratio_v1": r_score},
        reference_cutoff=None,
    )
    assert combined.strategy_score == pytest.approx(50.0)
