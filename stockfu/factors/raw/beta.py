"""low_beta:个股相对沪深300 的 β = cov(stock,bench)/var(bench)(window 日日收益)。

与 V1 `low_beta` operator 的 raw 口径逐行一致:120 日、qfq 收益、按日期交集对齐、
bench 进程内缓存。低 β → 高分由 profile(direction=lower_is_better)决定,本层只产 raw=β。
基准 sh000300 序列按 (as_of, span) 缓存——全A 回测每日全市场共享 1 次 DB,避免 N+1。
"""
from __future__ import annotations

from datetime import date

from stockfu.factors.raw import raw_fingerprint
from stockfu.scoring.contracts import MissingReason, RawFactorObservation

METRIC_ID = "low_beta"
_WINDOW = 120
_BENCH = "sh000300"

# bench 序列缓存:(as_of, span) -> (dates, closes)。回测内全A共享,每日仅 1 次 DB。
_BENCH_CACHE: dict[tuple[str, date, int], tuple[list, list]] = {}


def _bench_series(as_of: date, span: int, bench: str = _BENCH):
    from stockfu.services.factors import quote_series_dates

    key = (bench, as_of or date.today(), span)
    cached = _BENCH_CACHE.get(key)
    if cached is not None:
        return cached
    pair = quote_series_dates(bench, "close", span, as_of=as_of)
    _BENCH_CACHE[key] = pair
    if len(_BENCH_CACHE) > 64:                       # 上限保护(防跨多 as_of 膨胀)
        _BENCH_CACHE.pop(next(iter(_BENCH_CACHE)))
    return pair


def compute_low_beta(code: str, as_of: date, window: int = _WINDOW,
                     bench: str = _BENCH, price_basis: str = "qfq") -> RawFactorObservation:
    window = int(window)
    if window <= 0 or not bench:
        raise ValueError("low_beta 的 window/bench 参数无效")
    if price_basis != "qfq":
        raise ValueError("当前 low_beta 只支持 price_basis=qfq")
    fp = raw_fingerprint(
        METRIC_ID, "cov_over_var_vs_bench",
        {"window": window, "bench": bench, "price_basis": price_basis},
    )
    from stockfu.services.factors import quote_series_dates

    span = int(window * 1.5) + 30                    # 日历日缓冲(120 交易日≈180 日历日)
    s_dates, sc = quote_series_dates(code, "close", span, as_of=as_of)
    b_dates, bc = _bench_series(as_of, span, bench)
    # 按日期交集对齐:避免长度不等时末段截取错配 → β 失真
    smap = {d: v for d, v in zip(s_dates, sc) if v is not None}
    bmap = {d: v for d, v in zip(b_dates, bc) if v is not None}
    common = sorted(set(smap) & set(bmap))
    if len(common) < 21:
        return RawFactorObservation(
            asset_code=code, as_of=as_of, raw_metric_id=METRIC_ID,
            raw_value=None, raw_unit="beta", source_max_date=as_of,
            available_at=as_of, valid=False,
            missing_reason=MissingReason.INSUFFICIENT_SAMPLES, raw_fingerprint=fp,
            diagnostics={"n_common": len(common)})
    common = common[-(window + 1):]                  # 取末 window+1 个共同日
    sv = [smap[d] for d in common]
    bv = [bmap[d] for d in common]
    sr = [sv[i] / sv[i - 1] - 1 for i in range(1, len(sv))]
    br = [bv[i] / bv[i - 1] - 1 for i in range(1, len(bv))]
    m = len(sr)
    sm = sum(sr) / m
    bm = sum(br) / m
    cov = sum((sr[i] - sm) * (br[i] - bm) for i in range(m)) / m
    var = sum((b - bm) ** 2 for b in br) / m
    if var <= 0:
        return RawFactorObservation(
            asset_code=code, as_of=as_of, raw_metric_id=METRIC_ID,
            raw_value=None, raw_unit="beta", source_max_date=as_of,
            available_at=as_of, valid=False,
            missing_reason=MissingReason.NONTRADING, raw_fingerprint=fp,
            diagnostics={"n_common": len(common)})
    beta = cov / var
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=METRIC_ID,
        raw_value=float(beta), raw_unit="beta",
        source_max_date=as_of, available_at=as_of, valid=True, raw_fingerprint=fp,
        lookback_observations=window,
        diagnostics={"n_common": len(common), "bench": bench})
