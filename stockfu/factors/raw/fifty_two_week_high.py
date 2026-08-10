"""fifty_two_week_high:close / 近 N 日最高收盘(≈52 周),∈(0,1]。

George & Hwang (2004 JF):个股「距 52 周高点的近度」对未来收益的预测力强于
传统动量(锚定效应:近高点→前期套牢盘消化、上行阻力小)。→1 越接近新高;
direction=higher_is_better,本层只产 raw=比率(用 close_raw 的 max,含未复权高点语义,
与 George-Hwang 一致;close 用 qfq 亦等价比率,这里统一 qfq 与其他动量因子同源)。
"""
from __future__ import annotations

from datetime import date

from stockfu.factors.raw import raw_fingerprint
from stockfu.scoring.contracts import MissingReason, RawFactorObservation

METRIC_ID = "fifty_two_week_high"
_LOOKBACK = 250


def compute_fifty_two_week_high(code: str, as_of: date, lookback: int = _LOOKBACK,
                                price_basis: str = "qfq") -> RawFactorObservation:
    lookback = int(lookback)
    if lookback <= 0 or price_basis != "qfq":
        raise ValueError("fifty_two_week_high 的 lookback/price_basis 参数无效")
    fp = raw_fingerprint(METRIC_ID, "close_over_max_close",
                         {"lookback": lookback, "price_basis": price_basis})
    from stockfu.services.factors import quote_series

    span = int(lookback * 1.5) + 30
    closes = quote_series(code, "close", span, as_of=as_of)
    if len(closes) < 60:                          # 至少 ~3 个月才有意义
        return RawFactorObservation(
            asset_code=code, as_of=as_of, raw_metric_id=METRIC_ID,
            raw_value=None, raw_unit="ratio", source_max_date=as_of,
            available_at=as_of, valid=False,
            missing_reason=MissingReason.INSUFFICIENT_SAMPLES, raw_fingerprint=fp,
            diagnostics={"n_closes": len(closes)})
    window = closes[-lookback:] if len(closes) >= lookback else closes
    cur = closes[-1]
    high = max(window)
    if high <= 0 or cur <= 0:
        return RawFactorObservation(
            asset_code=code, as_of=as_of, raw_metric_id=METRIC_ID,
            raw_value=None, raw_unit="ratio", source_max_date=as_of,
            available_at=as_of, valid=False,
            missing_reason=MissingReason.NONTRADING, raw_fingerprint=fp,
            diagnostics={"n_closes": len(closes)})
    ratio = cur / high                            # (0,1],→1 越接近新高
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=METRIC_ID,
        raw_value=float(ratio), raw_unit="ratio",
        source_max_date=as_of, available_at=as_of, valid=True, raw_fingerprint=fp,
        lookback_observations=min(lookback, len(closes)),
        diagnostics={"ratio": round(ratio, 4), "lookback": lookback})
