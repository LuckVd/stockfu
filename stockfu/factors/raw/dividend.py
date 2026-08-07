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
                               price_basis: str = "raw") -> RawFactorObservation:
    trailing_days = int(trailing_days)
    if trailing_days <= 0:
        raise ValueError("dividend_yield_ttm.trailing_days 必须为正")
    if price_basis != "raw":
        raise ValueError("当前 dividend_yield_ttm 只支持 price_basis=raw")
    fp = raw_fingerprint(
        METRIC_ID, "ttm_cash_over_close_raw",
        {"trailing_days": trailing_days, "price_basis": price_basis},
    )
    from stockfu.services.dividend import dividend_yield_ttm

    res = dividend_yield_ttm(
        code, as_of=as_of, trailing_days=trailing_days, price_basis=price_basis)
    if res is None:
        return RawFactorObservation(
            asset_code=code, as_of=as_of, raw_metric_id=METRIC_ID,
            raw_value=None, raw_unit="percent", source_max_date=as_of,
            available_at=as_of, valid=False,
            missing_reason=MissingReason.NOT_DISCLOSED, raw_fingerprint=fp)
    y, ttm = res
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=METRIC_ID,
        raw_value=float(y), raw_unit="percent", source_max_date=as_of,
        available_at=as_of, valid=True, raw_fingerprint=fp,
        diagnostics={"ttm_per_share": float(ttm)})
