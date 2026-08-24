"""earnings_yield:盈利收益率 E/P = 100/PE_TTM(%,点时 PE)。

价值因子(Graham/AQR):E/P 把「贵不贵」拉回可比的收益率量纲,可与债券/股息横比。
PE<0(亏损)保留负的盈利收益率——负盈利是「差的盈利支撑」的有效负证据,不当缺失
(spec §11.1;profile knots 负锚点把它映射到 0 分附近而非中性 50);PE 为 None/0
(数据缺失或异常)才按缺失处理,由 profile 收缩到 50 + critical 门禁。
PE>0 时 raw=100/pe。direction=higher_is_better,本层只产 raw。
PE_TTM 来自 baostock 全字段 backfill 落入 quote_snapshot.pe。
"""
from __future__ import annotations

from datetime import date

from stockfu.factors.raw import raw_fingerprint
from stockfu.scoring.contracts import MissingReason, RawFactorObservation

METRIC_ID = "earnings_yield"


def compute_earnings_yield(code: str, as_of: date,
                           price_basis: str = "raw") -> RawFactorObservation:
    if price_basis != "raw":
        raise ValueError("earnings_yield 只支持 price_basis=raw(E/P 用未复权 PE)")
    fp = raw_fingerprint(METRIC_ID, "inverse_pe_ttm_x100",
                         {"price_basis": price_basis})
    from stockfu.services.valuation import pe_pb_at

    pe, _pb = pe_pb_at(code, as_of)
    if pe is None or pe == 0:
        return RawFactorObservation(
            asset_code=code, as_of=as_of, raw_metric_id=METRIC_ID,
            raw_value=None, raw_unit="percent", source_max_date=as_of,
            available_at=as_of, valid=False,
            missing_reason=MissingReason.NONPOSITIVE_DENOMINATOR, raw_fingerprint=fp,
            diagnostics={"pe": pe})
    # pe<0(亏损)→ 负 yield 是有效负证据(spec §11.1),不再当缺失
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=METRIC_ID,
        raw_value=float(100.0 / pe), raw_unit="percent",
        source_max_date=as_of, available_at=as_of, valid=True, raw_fingerprint=fp,
        diagnostics={"pe": round(pe, 4), "ep_pct": round(100.0 / pe, 4)})
