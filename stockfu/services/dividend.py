"""股息/分红服务：薄封装数据层，并负责把分红事件落库(按 ex_date 去重)。"""
from __future__ import annotations

from sqlmodel import select

from stockfu.data.manager import get_manager
from stockfu.db import session_scope
from stockfu.models import DividendEvent


def persist_dividends(code: str) -> int:
    """拉取分红事件并写入 dividend_event 表（按 ex_date 去重）。返回写入条数。"""
    metric = get_manager().get_dividend_metric(code)
    if not metric or not metric.events:
        return 0
    written = 0
    with session_scope() as s:
        existing = {e.ex_date for e in s.exec(
            select(DividendEvent).where(DividendEvent.asset_code == code)).all()}
        for e in metric.events:
            if e.ex_date in existing:
                continue
            s.add(DividendEvent(
                asset_code=code, ex_date=e.ex_date,
                record_date=e.record_date, announce_date=e.announce_date,
                per_share_cash=e.per_share_cash, currency=e.currency, source=e.source,
            ))
            written += 1
        s.commit()
    return written
