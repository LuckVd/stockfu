"""个股 PE/PB 历史分位(回测无未来函数用)。

baostock 全字段 backfill 已把 peTTM/pbMRQ 落入 quote_snapshot.pe/pb（个股基础表）。
本模块按 as_of 读 quote_snapshot <=as_of 序列用 factors.percentile 算分位，
本地、无网络、无未来函数。
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlmodel import select

from stockfu.db import session_scope
from stockfu.models import QuoteSnapshot
from stockfu.services import factors as F


def valuation_percentile(code: str, as_of: date, years: int = 5) -> tuple[float | None, float | None]:
    """读 quote_snapshot <=as_of 近 years 年序列，算 as_of 当天 PE/PB 分位(0-100)。

    本地、无网络、无未来函数(严格只用 <=as_of 数据)。取 <=as_of 最后一条当"当天值"；
    样本<10 或无当天值返回 (None, None)。quote_snapshot 拆表后已纯个股，天然过滤。
    """
    start = as_of - timedelta(days=years * 365 + 15)
    with session_scope() as s:
        rows = s.exec(select(QuoteSnapshot).where(
            QuoteSnapshot.asset_code == code,
            QuoteSnapshot.quote_date >= start,
            QuoteSnapshot.quote_date <= as_of,
        ).order_by(QuoteSnapshot.quote_date)).all()
    if not rows:
        return None, None
    pes = [r.pe for r in rows if r.pe and r.pe > 0]
    pbs = [r.pb for r in rows if r.pb and r.pb > 0]
    cur = rows[-1]  # <=as_of 的最后一条 = as_of 当天(或最近前一交易日)值
    pe_pct = F.percentile(pes, cur.pe)[0] if pes and cur.pe else None
    pb_pct = F.percentile(pbs, cur.pb)[0] if pbs and cur.pb else None
    return pe_pct, pb_pct
