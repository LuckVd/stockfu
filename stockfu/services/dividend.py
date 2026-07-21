"""股息/分红服务：薄封装数据层，并负责把分红事件落库(按 ex_date 去重)。"""
from __future__ import annotations

from datetime import date, timedelta

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


def dividend_yield_ttm(code: str, as_of=None) -> tuple[float, float] | None:
    """as_of 日 TTM 股息率(%):近 365 天每股现金分红 / as_of 日**不复权**收盘价 × 100。

    分红来自 dividend_event 表(baostock query_dividend_data / akshare 回补);
    **分母必须用 close_raw(不复权)**:名义现金 ÷ 全样本前复权价会虚高并引入 qfq 前视。
    严格 ex_date <= as_of 防未来函数。
    返回 (yield_pct, ttm_per_share) 或 None(无分红/无价/未回补 close_raw)。
    """
    from stockfu.services.factors import ADJ_RAW, quote_series

    ref = as_of or date.today()
    year_ago = ref - timedelta(days=365)
    with session_scope() as s:
        rows = s.exec(select(DividendEvent).where(
            DividendEvent.asset_code == code,
            DividendEvent.ex_date <= ref,
            DividendEvent.ex_date >= year_ago,
        )).all()
    if not rows:
        return None
    ttm = sum(r.per_share_cash for r in rows
              if r.per_share_cash and r.per_share_cash > 0)
    if ttm <= 0:
        return None
    # 不复权收盘;未回补 raw 时不回落 qfq(避免静默污染)
    closes = quote_series(code, "close", 10, as_of=ref, adj=ADJ_RAW)
    if not closes or closes[-1] <= 0:
        return None
    return round(ttm / closes[-1] * 100, 3), round(ttm, 4)
