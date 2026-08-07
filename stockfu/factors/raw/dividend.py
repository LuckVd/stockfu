"""dividend_yield_ttm:过去 365 天点时已实施每股现金分红 / 不复权价 × 100(§11.2)。

口径与现有 services.dividend_yield_ttm 一致(ex_date<=as_of 防未来、分母 close_raw)。
本层只产 raw_value,0–100 评分由 scoring 层的 profile 完成。
"""
from __future__ import annotations

from datetime import date

from stockfu.factors.raw import raw_fingerprint
from stockfu.scoring.contracts import MissingReason, RawFactorObservation

METRIC_ID = "dividend_yield_ttm"


def compute_dividend_yield_ttm(code: str, as_of: date, trailing_days: int = 365,
                               price_basis: str = "raw",
                               no_dividend_policy: str = "zero") -> RawFactorObservation:
    trailing_days = int(trailing_days)
    if trailing_days <= 0:
        raise ValueError("dividend_yield_ttm.trailing_days 必须为正")
    if price_basis != "raw":
        raise ValueError("当前 dividend_yield_ttm 只支持 price_basis=raw")
    if no_dividend_policy != "zero":
        raise ValueError("当前 dividend_yield_ttm.no_dividend_policy 只支持 zero")
    fp = raw_fingerprint(
        METRIC_ID, "ttm_cash_over_close_raw_zero_no_dividend",
        {"trailing_days": trailing_days, "price_basis": price_basis,
         "no_dividend_policy": no_dividend_policy},
    )
    from stockfu.services.dividend import dividend_yield_ttm_detail

    detail = dividend_yield_ttm_detail(
        code, as_of=as_of, trailing_days=trailing_days, price_basis=price_basis)
    if detail.ttm_cash_per_share <= 0:
        return RawFactorObservation(
            asset_code=code, as_of=as_of, raw_metric_id=METRIC_ID,
            raw_value=0.0, raw_unit="percent", source_max_date=as_of,
            available_at=as_of, valid=True, raw_fingerprint=fp,
            diagnostics={
                "ttm_per_share": 0.0,
                "dividend_event_count": detail.event_count,
                "no_cash_dividend": True,
            })
    if detail.yield_pct is None:
        reason = (MissingReason.NONPOSITIVE_DENOMINATOR
                  if detail.price_nonpositive else MissingReason.FIELD_MISSING)
        return RawFactorObservation(
            asset_code=code, as_of=as_of, raw_metric_id=METRIC_ID,
            raw_value=None, raw_unit="percent", source_max_date=as_of,
            available_at=as_of, valid=False, missing_reason=reason,
            raw_fingerprint=fp,
            diagnostics={
                "ttm_per_share": detail.ttm_cash_per_share,
                "dividend_event_count": detail.event_count,
                "price_missing": detail.price_missing,
                "price_nonpositive": detail.price_nonpositive,
            })
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=METRIC_ID,
        raw_value=float(detail.yield_pct), raw_unit="percent", source_max_date=as_of,
        available_at=as_of, valid=True, raw_fingerprint=fp,
        diagnostics={
            "ttm_per_share": detail.ttm_cash_per_share,
            "dividend_event_count": detail.event_count,
        })
