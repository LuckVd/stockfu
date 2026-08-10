"""momentum:N 日收益率(%,qfq),可跳过最近 skip 个交易日(Jegadeesh-Titman 12-1)。

经典横截面动量(Jegadeesh-Titman 1993):取过去 12 个月收益、剔除最近 1 个月
(避免短期反转污染)→ window=252、skip=21。高收益 → 高分由 profile
(direction=higher_is_better)决定;本层只产 raw=百分比收益。
skip=0 即普通 N 日动量(供反转/多因子复用同一 raw_metric)。
"""
from __future__ import annotations

from datetime import date

from stockfu.factors.raw import raw_fingerprint
from stockfu.scoring.contracts import MissingReason, RawFactorObservation

METRIC_ID = "momentum"


def compute_momentum(code: str, as_of: date, window: int = 252, skip: int = 21,
                     price_basis: str = "qfq") -> RawFactorObservation:
    window = int(window)
    skip = int(skip)
    if window <= 0 or skip < 0 or window <= skip:
        raise ValueError("momentum 的 window/skip 参数无效(window>skip>=0)")
    if price_basis != "qfq":
        raise ValueError("当前 momentum 只支持 price_basis=qfq")
    fp = raw_fingerprint(
        METRIC_ID, "pct_return_qfq",
        {"window": window, "skip": skip, "price_basis": price_basis},
    )
    from stockfu.services.factors import quote_series

    # quote_series 的 days 是日历日;交易日/日历日≈252/365,故放大 1.5 倍 + 缓冲。
    span = int((window + skip) * 1.5) + 30
    closes = quote_series(code, "close", span, as_of=as_of)
    need = window + skip + 1
    if len(closes) < need:
        return RawFactorObservation(
            asset_code=code, as_of=as_of, raw_metric_id=METRIC_ID,
            raw_value=None, raw_unit="percent", source_max_date=as_of,
            available_at=as_of, valid=False,
            missing_reason=MissingReason.INSUFFICIENT_SAMPLES, raw_fingerprint=fp,
            diagnostics={"n_closes": len(closes), "need": need})
    p_now = closes[-(1 + skip)]           # skip 日前收盘(12-1:跳过最近 1 月)
    p_old = closes[-(1 + skip + window)]  # window+skip 日前收盘
    if p_old <= 0 or p_now <= 0:
        return RawFactorObservation(
            asset_code=code, as_of=as_of, raw_metric_id=METRIC_ID,
            raw_value=None, raw_unit="percent", source_max_date=as_of,
            available_at=as_of, valid=False,
            missing_reason=MissingReason.NONTRADING, raw_fingerprint=fp,
            diagnostics={"n_closes": len(closes)})
    ret = (p_now / p_old - 1.0) * 100.0
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=METRIC_ID,
        raw_value=float(ret), raw_unit="percent",
        source_max_date=as_of, available_at=as_of, valid=True, raw_fingerprint=fp,
        lookback_observations=window,
        diagnostics={"window": window, "skip": skip, "ret_pct": round(ret, 4)})
