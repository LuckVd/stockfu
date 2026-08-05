"""大盘趋势 regime 门禁(_market_throttle_step)单测。

验证 trend 长均线滞回、vol 已实现波动率缩放、双信号 min 叠加、样本不足不拦。
纯函数测试,不依赖 DB。"""

from stockfu.backtest.engine import _market_throttle_step


def _flat(n: int, px: float = 100.0) -> list[float]:
    return [px] * n


# 默认 kwargs(除 bear_latched/ma_days/target_vol 外)
_KW = dict(enter_band=0.0, exit_band=0.03, bear_gross=0.50,
           vol_window=63, vol_floor=0.30, max_gross=1.0)


def test_trend_bear_entry_below_ma():
    w = _flat(200, 100.0)
    w[-1] = 99.0  # 跌破 MA100(< 100×(1-0))
    cap, bear = _market_throttle_step(w, bear_latched=False, ma_days=200,
                                      target_vol=None, **_KW)
    assert bear is True
    assert cap == 0.50


def test_trend_hysteresis_exit_needs_band():
    base = _flat(200, 100.0)
    # 涨到 102:未达 100×1.03=103 → 仍 bear(滞回)
    w = base[:-1] + [102.0]
    _, bear = _market_throttle_step(w, bear_latched=True, ma_days=200,
                                    target_vol=None, **_KW)
    assert bear is True
    # 涨到 103.1:> 103 → 退 bear,cap 恢复 max_gross
    w = base[:-1] + [103.1]
    cap, bear = _market_throttle_step(w, bear_latched=True, ma_days=200,
                                      target_vol=None, **_KW)
    assert bear is False
    assert cap == 1.0


def test_vol_high_vol_compresses_to_floor():
    # ±5% 交替 → 年化波动 ~79% >> target 0.15 → vscale 触 floor 0.30
    px = 100.0
    w = []
    for i in range(80):
        px *= (1.05 if i % 2 == 0 else 1 / 1.05)
        w.append(px)
    cap, _ = _market_throttle_step(w, bear_latched=False, ma_days=None,
                                   target_vol=0.15, **_KW)
    assert cap < 1.0
    assert cap >= 0.30  # vol_floor 兜底


def test_vol_low_vol_no_cap():
    # 平稳序列:波动 ~0 → realvol 极小 → vscale=min(1,大)=1 → 不拦
    cap, _ = _market_throttle_step(_flat(80, 100.0), bear_latched=False,
                                   ma_days=None, target_vol=0.15, **_KW)
    assert cap == 1.0


def test_min_combine_trend_and_vol():
    # trend bear(cap=0.50)+ 高 vol(floor 0.30)→ 取更严 min=0.30
    px = 100.0
    w = []
    for i in range(210):
        px *= (1.05 if i % 2 == 0 else 1 / 1.05)
        w.append(px)
    cap, bear = _market_throttle_step(w, bear_latched=False, ma_days=200,
                                      target_vol=0.15, **_KW)
    assert bear is True  # 末值跌破 MA → trend 进 bear
    assert cap <= 0.30 + 1e-9  # vol(0.30)比 trend(0.50)更严


def test_no_signal_no_cap():
    cap, bear = _market_throttle_step(_flat(200), bear_latched=False,
                                      ma_days=None, target_vol=None, **_KW)
    assert cap == 1.0
    assert bear is False


def test_insufficient_window_no_cap():
    # 窗口远不足 ma_days//4 → trend 不拦
    cap, _ = _market_throttle_step([100.0, 101.0, 99.0], bear_latched=False,
                                   ma_days=200, target_vol=None, **_KW)
    assert cap == 1.0
