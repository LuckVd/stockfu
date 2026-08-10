"""book_to_price:账面市值比 B/P = 1/PB(点时正 PB)。

Fama-French HML 价值因子的核心(B/M):B/P 高 = 相对净资产便宜。PB≤0(负净资产/异常)
按缺失处理,由 profile 收缩 + 门禁;PB>0 时 raw=1/pb。direction=higher_is_better,
本层只产 raw。PB 来自 baostock 全字段 backfill 落入 quote_snapshot.pb。
"""
from __future__ import annotations

from datetime import date

from stockfu.factors.raw import raw_fingerprint
from stockfu.scoring.contracts import MissingReason, RawFactorObservation

METRIC_ID = "book_to_price"


def compute_book_to_price(code: str, as_of: date,
                          price_basis: str = "raw") -> RawFactorObservation:
    if price_basis != "raw":
        raise ValueError("book_to_price 只支持 price_basis=raw(B/P 用未复权 PB)")
    fp = raw_fingerprint(METRIC_ID, "inverse_pb",
                         {"price_basis": price_basis})
    from stockfu.services.valuation import pe_pb_at

    _pe, pb = pe_pb_at(code, as_of)
    if pb is None or pb <= 0:
        return RawFactorObservation(
            asset_code=code, as_of=as_of, raw_metric_id=METRIC_ID,
            raw_value=None, raw_unit="ratio", source_max_date=as_of,
            available_at=as_of, valid=False,
            missing_reason=MissingReason.NONPOSITIVE_DENOMINATOR, raw_fingerprint=fp,
            diagnostics={"pb": pb})
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=METRIC_ID,
        raw_value=float(1.0 / pb), raw_unit="ratio",
        source_max_date=as_of, available_at=as_of, valid=True, raw_fingerprint=fp,
        diagnostics={"pb": round(pb, 4), "bp": round(1.0 / pb, 4)})
