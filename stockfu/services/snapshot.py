"""天级收盘快照读取门面。

看板 / 行情 / 网格 / 组合等所有读路径都走这里，不再直接调实时 get_quote。
- 读 quote_snapshot 最新一条（纯 DB，不联网）；
- 仅当「已过 daily_fetch_time 且是工作日 且该 code 今日无快照」时，按需请求一次
  （_upsert_quote 落盘，内部已含「今日已落盘跳过」），并用进程内防抖冷却避免
  短时间重复请求；
- 盘中 / 周末 / 未到 fetch_time → 返回最近一个交易日的快照（不请求）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlmodel import select

from stockfu.db import session_scope
from stockfu.models import Asset, QuoteSnapshot

_BEIJING = ZoneInfo("Asia/Shanghai")
_FETCH_COOLDOWN: dict[str, float] = {}   # {code: 上次按需请求的 monotonic 时间戳}
_TRADE_CAL: set[date] | None = None      # A股交易日历缓存（akshare，进程级）


@dataclass
class LatestSnapshot:
    code: str
    quote_date: date | None = None       # None = 库里完全没有该 code 任何历史
    close: float = 0.0
    pct_chg: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    amount: float | None = None
    pe: float | None = None
    pb: float | None = None
    market_cap: float | None = None
    name: str = ""
    currency: str = "CNY"


def beijing_now() -> datetime:
    return datetime.now(_BEIJING)


def beijing_today() -> date:
    return beijing_now().date()


def _past_fetch_time() -> bool:
    """当前北京时间是否已过 daily_fetch_time。"""
    from stockfu.config import get_daily_fetch_time
    h, m = (int(x) for x in get_daily_fetch_time().split(":"))
    now = beijing_now()
    return (now.hour, now.minute) >= (h, m)


def _is_weekday() -> bool:
    return beijing_now().weekday() < 5   # 周一0 … 周日6


def _read_latest(code: str) -> LatestSnapshot | None:
    """纯读：取 quote_snapshot 最新一条 + Asset 的 name/currency。"""
    with session_scope() as s:
        snap = s.exec(select(QuoteSnapshot).where(
            QuoteSnapshot.asset_code == code
        ).order_by(QuoteSnapshot.quote_date.desc()).limit(1)).first()
        a = s.get(Asset, code)
    if not snap:
        return None
    return LatestSnapshot(
        code=code, quote_date=snap.quote_date, close=snap.close or 0.0,
        pct_chg=snap.pct_chg, open=snap.open, high=snap.high, low=snap.low,
        volume=snap.volume, amount=snap.amount, pe=snap.pe, pb=snap.pb,
        market_cap=snap.market_cap,
        name=(a.name if a else "") or "",
        currency=(a.currency if a else "CNY"),
    )


def _try_fetch_today(code: str) -> None:
    """按需请求一次今日快照（_upsert_quote 内部已含「今日已落盘跳过」，幂等）。"""
    try:
        from stockfu.scheduler.jobs import _upsert_quote
        _upsert_quote(code)
    except Exception:  # noqa: BLE001
        pass


def ensure_name(code: str) -> str:
    """name 空则从数据源取一次回填 Asset.name（股票名是静态元数据，一次性，与行情天级无关）。"""
    with session_scope() as s:
        a = s.get(Asset, code)
        if a and a.name:
            return a.name
    try:
        from stockfu.data.manager import get_manager
        q = get_manager().get_quote(code)
        if q and q.name:
            with session_scope() as s:
                a = s.get(Asset, code)
                if a and not a.name:
                    a.name = q.name
                    s.commit()
            return q.name
    except Exception:  # noqa: BLE001
        pass
    return ""


def latest_snapshot(code: str, allow_fetch: bool = True) -> LatestSnapshot | None:
    """读最新天级快照。必要时按需补今日（防抖）。

    - 今日已落盘 → 返回今日；
    - 盘中 / 周末 / 未到 fetch_time → 返回最近一条历史（不请求）；
    - 已过 fetch_time 且工作日且今日无 → 请求一次（冷却内不重复），再读返回。
    """
    snap = _read_latest(code)
    if snap and not snap.name:
        snap.name = ensure_name(code)   # name 元数据回填（首次读 name 空的会联网取一次）
    today = beijing_today()
    if snap and snap.quote_date == today:
        return snap
    if allow_fetch and _past_fetch_time() and _is_weekday():
        from stockfu.config import get_fetch_retry_interval
        cooldown = get_fetch_retry_interval() * 60
        if time.monotonic() - _FETCH_COOLDOWN.get(code, 0.0) > cooldown:
            _FETCH_COOLDOWN[code] = time.monotonic()
            _try_fetch_today(code)
            snap = _read_latest(code)
    return snap


def _trade_calendar() -> set[date] | None:
    """A 股交易日历（akshare tool_trade_date_hist_sina），进程级缓存。失败返回 None。"""
    global _TRADE_CAL
    if _TRADE_CAL is not None:
        return _TRADE_CAL
    try:
        import pandas as pd
        from akshare import tool_trade_date_hist_sina
        _TRADE_CAL = {pd.Timestamp(t).date() for t in tool_trade_date_hist_sina()["trade_date"]}
        return _TRADE_CAL
    except Exception:  # noqa: BLE001
        return None


def latest_trade_date() -> date:
    """最近一个交易日（权威日历，自动跳过周末 + 法定节假日）。

    用 akshare 交易日历取 ≤ 今天(北京) 的最大交易日；日历不可用时
    fallback 到「日历回溯跳周末」。
    """
    today = beijing_today()
    cal = _trade_calendar()
    if cal:
        d = today
        for _ in range(15):           # 最多回溯 15 天（覆盖春节/国庆长假）
            if d in cal:
                return d
            d -= timedelta(days=1)
        return today                   # 兜底
    d = today                          # fallback：无日历，仅跳周末
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def index_quotes_view() -> dict:
    """三个大盘指数的当日点数/涨跌幅 + 恐/贪/热（/indices/quotes 与分享卡片共用）。

    情绪指数仅取 query_date 当天数据，不往回读旧数据（否则多日显示同一值）。
    上证取 market→MARKET；创业板/科创50 取 sector→板块名（与 compute_all 保存时一致）。
    pct_chg 优先用落盘值；backfill 未存时从最近两条 close 算。
    """
    from stockfu.models import IndexSnapshot
    td = date.today()
    cfg = {"000001": ("sh000001", "上证指数", "market", "MARKET"),
           "399006": ("sz399006", "创业板指", "sector", "创业板"),
           "000688": ("sh000688", "科创50", "sector", "科创50")}
    out: dict = {}
    with session_scope() as s:
        for c, (idx_code, name, lvl, scope) in cfg.items():
            snaps = s.exec(select(QuoteSnapshot).where(
                QuoteSnapshot.asset_code == idx_code
            ).order_by(QuoteSnapshot.quote_date.desc()).limit(2)).all()
            snap = snaps[0] if snaps else None
            prev_close = snaps[1].close if len(snaps) >= 2 else None
            # 当天有行情才显示价格/涨跌幅，否则留空（不展示隔夜旧数据）
            if snap and snap.quote_date == td:
                price = snap.close
                if snap.pct_chg is not None:
                    pct = snap.pct_chg
                elif snap.close and prev_close:
                    pct = round((snap.close / prev_close - 1) * 100, 2)
                else:
                    pct = None
            else:
                price, pct = None, None
            rows = s.exec(select(IndexSnapshot).where(
                IndexSnapshot.level == lvl, IndexSnapshot.scope == scope,
                IndexSnapshot.snap_date == td
            )).all()
            idx = {r.index_key: r.value for r in rows}
            out[c] = {"name": name, "price": price, "pct_chg": pct,
                      "fear": idx.get("fear"), "greed": idx.get("greed"), "heat": idx.get("heat")}
    return out
