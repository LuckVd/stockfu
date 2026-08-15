"""bollinger_pctb:周布林带位置 %b(0-1),对齐 weekly_bollinger 算子口径。

pct_b = (latest_close - lower) / (upper - lower),window=20 周、k=2。
贴下轨 → pct_b 小;贴上轨 → pct_b 大。方向由 profile 层决定(「低 %b 高分」用
direction=lower_is_better 的 profile)。指数资产专用:行情走 IndexQuoteDaily
(quote_model_for 已路由 sw 前缀),分数仓执行。
"""
from __future__ import annotations

from datetime import date

from stockfu.ai.operators.factors.weekly_bollinger import (
    _calc_bollinger,
    _weekly_series_from_pairs,
)
from stockfu.factors.raw import raw_fingerprint
from stockfu.scoring.contracts import MissingReason, RawFactorObservation
from stockfu.services.factors import quote_series_dates

METRIC_ID = "bollinger_pctb"
_ALGO = "weekly_bollinger_pctb"
# 与 probe 一致:约 6 年日线窗口(周聚合后约 300 根)
_MAX_BARS = 1500


def compute_bollinger_pctb(code: str, as_of: date, window: int = 20, k: float = 2.0,
                           ) -> RawFactorObservation:
    window = int(window)
    k = float(k)
    if window <= 0 or k <= 0:
        raise ValueError("bollinger_pctb 的 window/k 参数必须为正")
    fp = raw_fingerprint(
        METRIC_ID, _ALGO, {"window": window, "k": k},
    )
    span = int(_MAX_BARS * 1.5) + 30
    dates, closes = quote_series_dates(code, "close", span, as_of=as_of)
    closes = closes[-_MAX_BARS:]
    dates = dates[-_MAX_BARS:]
    n = len(closes)
    if n < 30:
        return RawFactorObservation(
            asset_code=code, as_of=as_of, raw_metric_id=METRIC_ID,
            raw_value=None, raw_unit="pct_b_0_1", source_max_date=as_of,
            available_at=as_of, valid=False,
            missing_reason=MissingReason.INSUFFICIENT_SAMPLES, raw_fingerprint=fp,
            diagnostics={"n_bars": n})
    weekly = _weekly_series_from_pairs(list(zip(dates, closes)))
    if len(weekly) < window:
        return RawFactorObservation(
            asset_code=code, as_of=as_of, raw_metric_id=METRIC_ID,
            raw_value=None, raw_unit="pct_b_0_1", source_max_date=as_of,
            available_at=as_of, valid=False,
            missing_reason=MissingReason.INSUFFICIENT_SAMPLES, raw_fingerprint=fp,
            diagnostics={"n_bars": n, "n_weekly": len(weekly)})
    sma, upper, lower, _bw = _calc_bollinger(weekly, window, k)
    latest = closes[-1]
    if (sma is None or upper is None or lower is None
            or upper <= lower or latest <= 0):
        return RawFactorObservation(
            asset_code=code, as_of=as_of, raw_metric_id=METRIC_ID,
            raw_value=None, raw_unit="pct_b_0_1", source_max_date=as_of,
            available_at=as_of, valid=False,
            missing_reason=MissingReason.NONTRADING, raw_fingerprint=fp,
            diagnostics={"n_bars": n, "n_weekly": len(weekly)})
    pct_b = (latest - lower) / (upper - lower)
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=METRIC_ID,
        raw_value=round(float(pct_b), 4), raw_unit="pct_b_0_1",
        source_max_date=as_of, available_at=as_of, valid=True, raw_fingerprint=fp,
        lookback_observations=len(weekly),
        diagnostics={"n_bars": n, "n_weekly": len(weekly), "pct_b": round(pct_b, 4)})
