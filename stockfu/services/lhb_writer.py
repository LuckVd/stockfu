"""龙虎榜事件入库统一收口（lhb_event 唯一写入入口）。

对齐 quote_writer 的 canonical writer 范式：cap_date 硬保证（超 cap 的事件一律
丢弃）、overwrite 语义、upsert 去重。**严禁别处直接 session.add(LhbEvent)**。

PIT 约定：榜单盘后披露——lhb_date 当日收盘后信息可见，T+1 可交易；raw 因子层
只读 lhb_date <= as_of 的事件即天然防未来。
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import delete
from sqlmodel import Session, select

from stockfu.models import LhbEvent


def _coerce_date(d: date | datetime | str) -> date:
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d)[:10])


def upsert_lhb_event(
    session: Session,
    rows: list[dict],
    *,
    cap_date: date | datetime | str,
    overwrite: bool = False,
) -> int:
    """批量写入龙虎榜事件；返回新增条数。

    唯一键 (asset_code, lhb_date, reason)。overwrite=False(默认)跳过已有键；
    True 则覆盖净额/机构家数等可变字段。lhb_date > cap_date 一律跳过。
    """
    cap = _coerce_date(cap_date)
    if not rows:
        return 0
    existing = {
        (r.asset_code, r.lhb_date, r.reason): r for r in session.exec(
            select(LhbEvent).where(
                LhbEvent.asset_code.in_({r["asset_code"] for r in rows}),
                LhbEvent.lhb_date >= min(r["lhb_date"] for r in rows),
            )
        ).all()
    }
    n = 0
    skipped_cap = 0
    for r in rows:
        d = r["lhb_date"]
        if d > cap:
            skipped_cap += 1
            continue
        key = (r["asset_code"], d, r.get("reason", ""))
        row = existing.get(key)
        if row is None:
            session.add(LhbEvent(**r))
            n += 1
        elif overwrite:
            for f in ("buy_amount", "sell_amount", "net_amount", "net_ratio",
                      "close", "pct_chg", "turnover", "float_mktcap",
                      "inst_buy_count", "inst_sell_count", "success_rate"):
                if f in r:
                    setattr(row, f, r[f])
            session.add(row)
            n += 1
    if skipped_cap:
        pass  # 丢弃超 cap 事件（cap_date 硬保证，调用方日志可见）
    return n


def clear_lhb_events(session: Session, codes: list[str] | None = None) -> int:
    """清空龙虎榜事件（--full 重抓前用）。返回删除条数。"""
    stmt = delete(LhbEvent)
    if codes:
        stmt = stmt.where(LhbEvent.asset_code.in_(codes))
    result = session.exec(stmt)
    return result.rowcount or 0
