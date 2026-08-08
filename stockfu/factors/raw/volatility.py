"""low_volatility_20d:近 20 个交易日收益标准差 × √252 年化(%,qfq 价格)。

与现有 low_volatility operator 的 raw 口径一致(总体方差 /n、qfq 收益)。
低波动 → score 高由 profile(direction=lower_is_better)决定,本层只产 raw。
"""
from __future__ import annotations

import math
from datetime import date

from stockfu.factors.raw import raw_fingerprint
from stockfu.scoring.contracts import MissingReason, RawFactorObservation

METRIC_ID = "low_volatility_20d"
_WINDOW = 20


def compute_low_volatility_20d(code: str, as_of: date, window: int = _WINDOW,
                               price_basis: str = "qfq", variance_ddof: int = 0) -> RawFactorObservation:
    window = int(window)
    variance_ddof = int(variance_ddof)
    if window <= 0 or variance_ddof not in (0, 1) or window <= variance_ddof:
        raise ValueError("low_volatility_20d 的 window/ddof 参数无效")
    if price_basis != "qfq":
        raise ValueError("当前 low_volatility_20d 只支持 price_basis=qfq")
    fp = raw_fingerprint(
        METRIC_ID, "std_ret_x_sqrt252_x100",
        {"window": window, "price_basis": price_basis, "variance_ddof": variance_ddof},
    )
    from stockfu.services.factors import quote_series

    # window+12 留 lookback 余量(quote_series 按历史日截窗口)
    closes = quote_series(code, "close", window + 12, as_of=as_of, adj="qfq")
    if len(closes) < window + 1:
        return RawFactorObservation(
            asset_code=code, as_of=as_of, raw_metric_id=METRIC_ID,
            raw_value=None, raw_unit="annualized_vol_percent", source_max_date=as_of,
            available_at=as_of, valid=False,
            missing_reason=MissingReason.INSUFFICIENT_SAMPLES, raw_fingerprint=fp,
            diagnostics={"n_closes": len(closes)})
    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]
    recent = rets[-window:]
    mean = sum(recent) / window
    var = sum((r - mean) ** 2 for r in recent) / (window - variance_ddof)
    std = math.sqrt(max(var, 0.0))
    if std <= 0.0:
        # 价格恒定/停牌全 0 收益 → 无信息(防把停牌误判为极低波)
        return RawFactorObservation(
            asset_code=code, as_of=as_of, raw_metric_id=METRIC_ID,
            raw_value=None, raw_unit="annualized_vol_percent", source_max_date=as_of,
            available_at=as_of, valid=False,
            missing_reason=MissingReason.NONTRADING, raw_fingerprint=fp,
            diagnostics={"n_closes": len(closes)})
    annual = std * math.sqrt(252.0) * 100.0
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=METRIC_ID,
        raw_value=float(annual), raw_unit="annualized_vol_percent",
        source_max_date=as_of, available_at=as_of, valid=True, raw_fingerprint=fp,
        lookback_observations=window,
        diagnostics={"daily_std_pct": round(std * 100.0, 4), "n": window})
