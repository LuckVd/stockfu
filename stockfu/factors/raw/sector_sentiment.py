"""sector_sentiment:行业指数情绪分位(fear/greed/heat),对齐 composite 口径。

值域均为 0-100 的自身历史分位(rolling 窗口),与 probes/sector_rotation.py 的
compute_sentiment 完全同口径:
  fear  = mean(20 日波动率分位, 100 - 5 日涨幅分位)   高恐 → 高分
  greed = 5 日涨幅分位                                高贪 → 高分
  heat  = 量能相对前 20 日均量倍数的分位               高热 → 高分

方向由 profile 层决定(如「低 greed」用 direction=lower_is_better 的 profile)。
指数资产专用:行情走 IndexQuoteDaily(quote_model_for 已路由 sw 前缀)。
"""
from __future__ import annotations

from datetime import date

from stockfu.factors.raw import raw_fingerprint
from stockfu.scoring.contracts import MissingReason, RawFactorObservation
from stockfu.services.composite import (
    _rolling_chg,
    _rolling_relative_activity,
    _rolling_vol,
)
from stockfu.services.factors import percentile, quote_series

_METRIC_PREFIX = "sector_sentiment"
_ALGO = "percentile_self_rolling"
# 与 probe 一致:约 6 年日线(1500 根)窗口内取自身分位
_MAX_BARS = 1500


def _series(code: str, as_of: date, field: str) -> list[float]:
    span = int(_MAX_BARS * 1.5) + 30
    return quote_series(code, field, span, as_of=as_of)[-_MAX_BARS:]


def _fear_from(closes: list[float], vol_window: int, chg_window: int) -> float | None:
    parts: list[float] = []
    vols = _rolling_vol(closes, vol_window)
    if vols:
        p = percentile(vols, vols[-1])[0]
        if p is not None:
            parts.append(float(p))               # 高波 → fear
    chgs = _rolling_chg(closes, chg_window)
    if chgs:
        p = percentile(chgs, chgs[-1])[0]
        if p is not None:
            parts.append(100.0 - float(p))       # 跌 → fear
    if not parts:
        return None
    return round(sum(parts) / len(parts), 2)


def _greed_from(closes: list[float], chg_window: int) -> float | None:
    chgs = _rolling_chg(closes, chg_window)
    if not chgs:
        return None
    p = percentile(chgs, chgs[-1])[0]
    return round(float(p), 2) if p is not None else None


def _heat_from(amounts: list[float], act_window: int) -> float | None:
    acts = _rolling_relative_activity(amounts, act_window)
    if not acts:
        return None
    p = percentile(acts, acts[-1])[0]
    return round(float(p), 2) if p is not None else None


def _mk(code: str, as_of: date, metric_id: str, value: float | None,
        algo_params: dict, fp: str, n: int) -> RawFactorObservation:
    if value is None:
        return RawFactorObservation(
            asset_code=code, as_of=as_of, raw_metric_id=metric_id,
            raw_value=None, raw_unit="percentile_0_100", source_max_date=as_of,
            available_at=as_of, valid=False,
            missing_reason=MissingReason.INSUFFICIENT_SAMPLES, raw_fingerprint=fp,
            diagnostics={"n_bars": n})
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=metric_id,
        raw_value=float(value), raw_unit="percentile_0_100",
        source_max_date=as_of, available_at=as_of, valid=True, raw_fingerprint=fp,
        lookback_observations=_MAX_BARS,
        diagnostics={"n_bars": n, "value": value})


def compute_sector_fear(code: str, as_of: date, vol_window: int = 20,
                        chg_window: int = 5, metric_id: str = "sector_fear",
                        ) -> RawFactorObservation:
    vol_window = int(vol_window)
    chg_window = int(chg_window)
    if vol_window <= 0 or chg_window <= 0:
        raise ValueError("sector_fear 的窗口参数必须为正")
    fp = raw_fingerprint(metric_id, _ALGO,
                         {"vol_window": vol_window, "chg_window": chg_window})
    closes = _series(code, as_of, "close")
    return _mk(code, as_of, metric_id,
               _fear_from(closes, vol_window, chg_window),
               {"vol_window": vol_window, "chg_window": chg_window},
               fp, len(closes))


def compute_sector_greed(code: str, as_of: date, chg_window: int = 5,
                         metric_id: str = "sector_greed",
                         ) -> RawFactorObservation:
    chg_window = int(chg_window)
    if chg_window <= 0:
        raise ValueError("sector_greed 的窗口参数必须为正")
    fp = raw_fingerprint(metric_id, _ALGO, {"chg_window": chg_window})
    closes = _series(code, as_of, "close")
    return _mk(code, as_of, metric_id, _greed_from(closes, chg_window),
               {"chg_window": chg_window}, fp, len(closes))


def compute_sector_heat(code: str, as_of: date, act_window: int = 20,
                        metric_id: str = "sector_heat",
                        ) -> RawFactorObservation:
    act_window = int(act_window)
    if act_window <= 0:
        raise ValueError("sector_heat 的窗口参数必须为正")
    fp = raw_fingerprint(metric_id, _ALGO, {"act_window": act_window})
    amounts = _series(code, as_of, "amount")
    return _mk(code, as_of, metric_id, _heat_from(amounts, act_window),
               {"act_window": act_window}, fp, len(amounts))
