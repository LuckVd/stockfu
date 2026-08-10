"""trend_linearity:价格对时间线性回归的 signed_r² = sign(slope)·r² ∈ [-1,1]。

趋势跟踪滤波:只追「涨得平稳线性」的趋势(r²→1 且 slope>0),滤掉脉冲式冲高
(动量高但 r² 低 = 鱼尾/伪强势)。与 momentum 正交——momentum 看「涨多少」,
trend_linearity 看「涨得稳不稳」。direction=higher_is_better,本层只产 raw。
"""
from __future__ import annotations

from datetime import date

from stockfu.factors.raw import raw_fingerprint
from stockfu.scoring.contracts import MissingReason, RawFactorObservation

METRIC_ID = "trend_linearity"


def compute_trend_linearity(code: str, as_of: date, window: int = 60,
                             price_basis: str = "qfq") -> RawFactorObservation:
    window = int(window)
    if window < 3 or price_basis != "qfq":
        raise ValueError("trend_linearity 的 window/price_basis 参数无效")
    fp = raw_fingerprint(METRIC_ID, "signed_r2_linreg",
                         {"window": window, "price_basis": price_basis})
    from stockfu.services.factors import linreg_r2, quote_series

    span = int(window * 1.5) + 30
    closes = quote_series(code, "close", span, as_of=as_of)
    if len(closes) < window:
        return RawFactorObservation(
            asset_code=code, as_of=as_of, raw_metric_id=METRIC_ID,
            raw_value=None, raw_unit="signed_r2", source_max_date=as_of,
            available_at=as_of, valid=False,
            missing_reason=MissingReason.INSUFFICIENT_SAMPLES, raw_fingerprint=fp,
            diagnostics={"n_closes": len(closes)})
    r2, slope = linreg_r2(closes[-window:])
    direction = 1.0 if slope > 0 else -1.0
    signed = r2 * direction
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=METRIC_ID,
        raw_value=float(signed), raw_unit="signed_r2",
        source_max_date=as_of, available_at=as_of, valid=True, raw_fingerprint=fp,
        lookback_observations=window,
        diagnostics={"r2": round(r2, 4), "slope": round(slope, 6), "window": window})
