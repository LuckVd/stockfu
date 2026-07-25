"""股息/分红服务：薄封装数据层，并负责把分红事件落库(按 ex_date 去重)。

读路径默认 **优先本地 dividend_event 表**（邮件/看板不再为 TTM 股息打 baostock）。
写路径 ``persist_dividends`` 强制联网刷新后落库。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from sqlmodel import select

from stockfu.data.base import DividendEventDTO, DividendMetric
from stockfu.data.manager import get_manager
from stockfu.db import session_scope
from stockfu.models import DividendEvent


# 回测分红供给器：engine 每次 run 批量预载 dividend_event 后挂载。
# fn(code, start, ref) -> list[(ex_date, per_share_cash)] | None；None 回退 DB。
_BT_DIVIDEND_PROVIDER = None


def set_backtest_dividend_provider(fn) -> None:
    """挂载回测 TTM 分红内存供给器。"""
    global _BT_DIVIDEND_PROVIDER
    _BT_DIVIDEND_PROVIDER = fn


def clear_backtest_dividend_provider() -> None:
    """摘除回测分红供给器，恢复 live 数据库读取。"""
    global _BT_DIVIDEND_PROVIDER
    _BT_DIVIDEND_PROVIDER = None


def metric_from_db(
    code: str,
    latest_price: Optional[float] = None,
    *,
    years: int = 10,
) -> Optional[DividendMetric]:
    """从 ``dividend_event`` 表组装 DividendMetric；无事件返回 None。

    TTM = 近 365 天每股现金分红合计；股息率 = TTM / latest_price × 100（有价时）。
    """
    this_year = date.today().year
    start_year = this_year - years + 1
    # 覆盖跨年除权：取 start_year-01-01 起全部事件
    start_floor = date(start_year, 1, 1)
    with session_scope() as s:
        rows = s.exec(
            select(DividendEvent)
            .where(
                DividendEvent.asset_code == code,
                DividendEvent.ex_date >= start_floor,
            )
            .order_by(DividendEvent.ex_date)
        ).all()
    if not rows:
        return None
    events = [
        DividendEventDTO(
            ex_date=r.ex_date,
            per_share_cash=float(r.per_share_cash or 0),
            record_date=r.record_date,
            announce_date=r.announce_date,
            currency=r.currency or "CNY",
            source=r.source or "db:dividend_event",
        )
        for r in rows
        if r.ex_date and r.per_share_cash and float(r.per_share_cash) > 0
    ]
    if not events:
        return None
    ref = date.today()
    ttm = sum(
        e.per_share_cash for e in events
        if ref - timedelta(days=365) <= e.ex_date <= ref   # 上下界：排除未除权的 future ex_date（防未来函数）
    )
    ttm_yield = (
        round(ttm / latest_price * 100, 2)
        if latest_price and latest_price > 0 and ttm > 0
        else None
    )
    cur = events[-1].currency if events else "CNY"
    return DividendMetric(
        code=code,
        currency=cur,
        ttm_cash_per_share=round(ttm, 4),
        ttm_yield_pct=ttm_yield,
        events=events,
        coverage=f"db:{start_year}-{this_year}({len(events)}次)",
    )


def persist_dividends(code: str, *, years: int = 10, timeout: float = 10.0) -> int:
    """拉取分红事件并写入 dividend_event 表（按 ex_date 去重）。返回写入条数。

    强制联网（force_network），不走「仅库」短路，否则无法回补。
    """
    metric = get_manager().get_dividend_metric(
        code, force_network=True, years=years, timeout=timeout,
    )
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
            existing.add(e.ex_date)   # 同批去重：防 metric.events 自身重复（如 baostock 同 ex_date 双行）
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
    rows: list[tuple[date, float | None]] | None = None
    if _BT_DIVIDEND_PROVIDER is not None:
        rows = _BT_DIVIDEND_PROVIDER(code, year_ago, ref)
    if rows is None:
        with session_scope() as s:
            db_rows = s.exec(select(DividendEvent).where(
                DividendEvent.asset_code == code,
                DividendEvent.ex_date <= ref,
                DividendEvent.ex_date >= year_ago,
            )).all()
        rows = [(r.ex_date, r.per_share_cash) for r in db_rows]
    if not rows:
        return None
    ttm = sum(cash for _ex_date, cash in rows if cash and cash > 0)
    if ttm <= 0:
        return None
    # 不复权收盘;未回补 raw 时不回落 qfq(避免静默污染)
    closes = quote_series(code, "close", 10, as_of=ref, adj=ADJ_RAW)
    if not closes or closes[-1] <= 0:
        return None
    return round(ttm / closes[-1] * 100, 3), round(ttm, 4)
