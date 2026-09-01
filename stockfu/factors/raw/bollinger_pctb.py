"""bollinger_pctb:周布林带位置 %b(0-1)。

pct_b = (latest_close - lower) / (upper - lower),window=20 周、k=2。
贴下轨 → pct_b 小;贴上轨 → pct_b 大。方向由 profile 层决定(「低 %b 高分」用
direction=lower_is_better 的 profile)。指数资产专用:行情走 IndexQuoteDaily
(quote_model_for 已路由 sw 前缀),分数仓执行。

周聚合与布林带计算原属 V1 weekly_bollinger 算子(V1 已移除,配置归档见
docs/legacy/strategy-v1/);纯函数随迁移至此,口径不变。
"""
from __future__ import annotations

import math
from datetime import date

from stockfu.factors.raw import raw_fingerprint
from stockfu.scoring.contracts import MissingReason, RawFactorObservation
from stockfu.services.factors import quote_series_dates

METRIC_ID = "bollinger_pctb"
_ALGO = "weekly_bollinger_pctb"
# 与 probe 一致:约 6 年日线窗口(周聚合后约 300 根)
_MAX_BARS = 1500


def _weekly_series_from_pairs(pairs) -> list[float]:
    """(date, close) 升序对 → 周度收盘价序列(每周最后交易日 close,后覆盖前)。

    周聚合与原 rows 版逐值一致:按 ISO 周,同周后写覆盖前写 → 取该周最后交易日。
    """
    weekly: dict[tuple[int, int], float] = {}  # (iso_year, iso_week) -> close
    for d, c in pairs:
        if c > 0:
            iso = d.isocalendar()
            weekly[(iso[0], iso[1])] = c
    return [weekly[k] for k in sorted(weekly.keys())]


def _calc_bollinger(series: list[float], window: int, k: float):
    """计算布林带,返回最新的 (sma, upper, lower, bandwidth)。"""
    if len(series) < window:
        return None, None, None, None
    recent = series[-window:]
    sma = sum(recent) / window
    variance = sum((x - sma) ** 2 for x in recent) / window
    std = math.sqrt(variance)
    upper = sma + k * std
    lower = sma - k * std
    bandwidth = (upper - lower) / sma * 100 if sma > 0 else 0.0
    return sma, upper, lower, bandwidth


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
