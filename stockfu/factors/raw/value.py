"""value:PE 历史分位(0-100,近 years 年,<=as_of 点时、无未来函数)。

与 V1 `value` operator 的 raw 口径一致——复用同一 `services.valuation.valuation_percentile`。
低估(分位低)→ 高分由 profile(direction=lower_is_better)决定,本层只产 raw=PE 分位。
"""
from __future__ import annotations

from datetime import date

from stockfu.factors.raw import raw_fingerprint
from stockfu.scoring.contracts import MissingReason, RawFactorObservation

METRIC_ID = "value"
_YEARS = 5


def compute_value(code: str, as_of: date, years: int = _YEARS) -> RawFactorObservation:
    years = int(years)
    if years <= 0:
        raise ValueError("value.years 必须为正")
    fp = raw_fingerprint(METRIC_ID, "pe_percentile", {"years": years})
    from stockfu.services.valuation import valuation_percentile

    pct, pb = valuation_percentile(code, as_of, years=years)
    if pct is None:
        return RawFactorObservation(
            asset_code=code, as_of=as_of, raw_metric_id=METRIC_ID,
            raw_value=None, raw_unit="pe_percentile", source_max_date=as_of,
            available_at=as_of, valid=False,
            missing_reason=MissingReason.INSUFFICIENT_SAMPLES, raw_fingerprint=fp)
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=METRIC_ID,
        raw_value=float(pct), raw_unit="pe_percentile",
        source_max_date=as_of, available_at=as_of, valid=True, raw_fingerprint=fp,
        diagnostics={"pb_pct": None if pb is None else float(pb)})
