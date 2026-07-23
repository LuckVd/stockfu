"""行情入库**唯一收口**。

三张行情表各只有一个 canonical writer：
  - upsert_quote_snapshot  → quote_snapshot（个股/港美股/黄金，三复权）
  - upsert_etf_daily        → etf_quote_daily（ETF 前复权）
  - upsert_index_daily      → index_quote_daily（指数/申万行业）

所有 writer 共同保证「**根据日期**」：
  1. 任何 quote_date > cap_date 的源 bar 一律丢弃（永不写未来日）；
  2. cap_date 由调用方传入（fetch=目标交易日、backfill=--end）；
  3. cap_date 本身由 validate_ingest_date 校验——未来日 / 当日未收盘 / 非交易日直接报错。

本模块只放**纯叶子逻辑**（不依赖 scheduler.jobs / scheduler.backfill_adj_prices），
从而打断原 jobs ↔ backfill_adj_prices 经由 _apply_bar_full 的循环引用。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from enum import Enum
from typing import Any

from sqlmodel import Session, select

from stockfu.models import EtfQuoteDaily, IndexQuoteDaily, QuoteSnapshot


# ───────────────────────── 日期权威 ─────────────────────────

def _ingest_cutoff() -> dtime:
    """A 股收盘分界（15:00 收盘 + 缓冲）。env STOCKFU_INGEST_CUTOFF=HH:MM 可覆盖。"""
    raw = os.environ.get("STOCKFU_INGEST_CUTOFF", "16:00")
    h, m = raw.split(":")
    return dtime(int(h), int(m))


def _coerce_date(d: date | datetime | str) -> date:
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d))


def validate_ingest_date(target: date | datetime | str, *, now: datetime | None = None) -> date:
    """校验入库目标日。非法即 raise ValueError，合法返回 date。

    - 未来日（> 北京今天）→ 报错；
    - == 北京今天 且未过收盘分界 → 报错（当日尚未收盘）；
    - 非交易日（交易日历可用时按日历判；不可用时按周末判）→ 报错。
    """
    from stockfu.services.snapshot import _trade_calendar, beijing_now

    target = _coerce_date(target)
    n = now or beijing_now()
    today = n.date()
    if target > today:
        raise ValueError(f"目标日期 {target} 是未来日期（今天 {today}），拒绝入库")
    if target == today and n.time() < _ingest_cutoff():
        raise ValueError(
            f"目标日期 {target} 当日尚未收盘（收盘分界 {_ingest_cutoff()}），拒绝入库"
        )
    cal = _trade_calendar()
    if cal is not None:
        if target not in cal:
            raise ValueError(f"目标日期 {target} 非交易日（不在 A 股交易日历）")
    elif target.weekday() >= 5:
        raise ValueError(f"目标日期 {target} 非交易日（周末；交易日历不可用，按周末判）")
    return target


def latest_trade_date_on_or_before(d: date | datetime | str) -> date:
    """<= d 的最近一个 A 股交易日（供 --schedule 推导 + 读路径替代 today 锚定）。

    交易日历可用时按日历；不可用（离线）时回退到「跳周末」。
    """
    from stockfu.services.snapshot import _trade_calendar

    d = _coerce_date(d)
    cal = _trade_calendar()
    if cal is not None:
        x = d
        for _ in range(25):  # 覆盖春节/国庆长假
            if x in cal:
                return x
            x -= timedelta(days=1)
        return d
    x = d
    while x.weekday() >= 5:
        x -= timedelta(days=1)
    return x


def latest_closed_trade_day(*, now: datetime | None = None) -> date:
    """最近一个**已收盘**的交易日：now 过收盘分界取今天，否则取昨天；再回退到真实交易日。

    供 --schedule / Web 按需推导目标日——凌晨/盘前自动落到前一收盘交易日，
    避免 validate_ingest_date 把「今天」判为未收盘而跳过。
    """
    from stockfu.services.snapshot import beijing_now

    n = now or beijing_now()
    eff = n.date() if n.time() >= _ingest_cutoff() else n.date() - timedelta(days=1)
    return latest_trade_date_on_or_before(eff)


# ───────────────────────── 叶子：单行写入 ─────────────────────────

def _bar_pct(b, prev_close: float | None) -> float | None:
    """涨跌幅%：优先 bar.pct_chg，否则用 prev_close 推。"""
    if getattr(b, "pct_chg", None) is not None:
        return float(b.pct_chg)
    if prev_close and prev_close > 0 and b.close:
        return round((b.close / prev_close - 1) * 100, 2)
    return None


def _apply_bar_full(snap: QuoteSnapshot, b, prev_close: float | None = None,
                    adj: str = "qfq") -> None:
    """把一根 K 线写入 snapshot。

    adj=qfq(默认)：写遗留 open/high/low/close **与** open_qfq/…/close_qfq，并写
    volume/amount/状态/估值（与复权无关的字段只在 qfq 路径更新，避免 raw/hfq 覆盖）。
    adj=raw|hfq：只写对应 *_raw / *_hfq OHLC。
    """
    adj_n = (adj or "qfq").lower()
    o = float(b.open) if b.open is not None else None
    h = float(b.high) if b.high is not None else None
    l = float(b.low) if b.low is not None else None
    c = float(b.close) if b.close is not None else None
    if adj_n == "raw":
        if o is not None:
            snap.open_raw = o
        if h is not None:
            snap.high_raw = h
        if l is not None:
            snap.low_raw = l
        if c is not None:
            snap.close_raw = c
        return
    if adj_n == "hfq":
        if o is not None:
            snap.open_hfq = o
        if h is not None:
            snap.high_hfq = h
        if l is not None:
            snap.low_hfq = l
        if c is not None:
            snap.close_hfq = c
        return
    # qfq + 遗留别名同步
    snap.open = o
    snap.high = h
    snap.low = l
    snap.close = c if c is not None else (snap.close or 0.0)
    snap.open_qfq = o
    snap.high_qfq = h
    snap.low_qfq = l
    snap.close_qfq = c
    if b.volume is not None:
        snap.volume = b.volume
    if b.amount is not None:
        snap.amount = b.amount
    pct = _bar_pct(b, prev_close)
    if pct is not None:
        snap.pct_chg = pct
    if getattr(b, "trade_status", None) is not None:
        snap.trade_status = int(b.trade_status)
    if getattr(b, "is_st", None) is not None:
        snap.is_st = int(b.is_st)
    if getattr(b, "pe", None) is not None:
        snap.pe = float(b.pe)
    if getattr(b, "pb", None) is not None:
        snap.pb = float(b.pb)
    if getattr(b, "turnover", None) is not None:
        snap.turnover = float(b.turnover)


def _patch_status_only(snap: QuoteSnapshot, b) -> bool:
    """仅补空的 is_st/trade_status。返回是否有改动。"""
    ch = False
    if snap.trade_status is None and getattr(b, "trade_status", None) is not None:
        snap.trade_status = int(b.trade_status)
        ch = True
    if snap.is_st is None and getattr(b, "is_st", None) is not None:
        snap.is_st = int(b.is_st)
        ch = True
    return ch


# ───────────────────────── quote_snapshot writer ─────────────────────────

class WritePolicy(str, Enum):
    MERGE_ADJ = "merge_adj"        # baostock 三复权：合并 qfq/raw/hfq 列进同一行
    FULL_QFQ = "full_qfq"          # 全量 OHLCV+状态+估值覆盖（qfq），含 extras
    PATCH_STATUS = "patch_status"  # 仅补空 is_st/trade_status


@dataclass
class QuotePayload:
    """单日行情载荷。qfq/raw/hfq 为 KlineBar（或 None）；policy 可覆盖 writer 默认。"""
    qfq: Any = None
    raw: Any = None
    hfq: Any = None
    policy: WritePolicy | None = None
    extras: dict = field(default_factory=dict)  # pe/pb/market_cap/turnover 覆盖值（FULL_QFQ 时写）


def upsert_quote_snapshot(
    session: Session,
    code: str,
    payload_by_date: dict[date, QuotePayload],
    *,
    policy: WritePolicy,
    cap_date: date | datetime | str,
    preserve_qfq: bool = True,
) -> int:
    """quote_snapshot **唯一**写入入口。返回写入（新增+更新）行数。

    硬保证：quote_date > cap_date 的 bar 一律跳过（永不写未来日）。
    新行（库内无该日）无论 policy 如何，均按 FULL_QFQ 全量插入（空行无法 patch）。
    """
    cap = _coerce_date(cap_date)
    if not payload_by_date:
        return 0
    existing = {
        q.quote_date: q for q in session.exec(
            select(QuoteSnapshot).where(QuoteSnapshot.asset_code == code)
        ).all()
    }
    n = 0
    skipped_future = 0
    prev_qfq_close: float | None = None
    for d in sorted(payload_by_date):
        if d > cap:
            skipped_future += 1
            continue
        pl = payload_by_date[d]
        pol = pl.policy or policy
        is_new = d not in existing
        if pol == WritePolicy.PATCH_STATUS and is_new:
            pol = WritePolicy.FULL_QFQ  # 新行必须全量插入
        snap = existing.get(d) or QuoteSnapshot(asset_code=code, quote_date=d)

        if pol == WritePolicy.MERGE_ADJ:
            has_qfq = (snap.close_qfq is not None) or (
                snap.close is not None and float(snap.close or 0) > 0
            )
            if pl.qfq is not None and (not preserve_qfq or not has_qfq):
                _apply_bar_full(snap, pl.qfq, prev_qfq_close, adj="qfq")
                prev_qfq_close = pl.qfq.close or prev_qfq_close
            elif pl.qfq is not None and pl.qfq.close:
                prev_qfq_close = pl.qfq.close
            if pl.raw is not None:
                _apply_bar_full(snap, pl.raw, None, adj="raw")
            if pl.hfq is not None:
                _apply_bar_full(snap, pl.hfq, None, adj="hfq")
            if is_new and (snap.close is None or snap.close == 0):
                if snap.close_raw is not None:
                    snap.close = float(snap.close_raw)
                elif pl.qfq is not None and pl.qfq.close:
                    snap.close = float(pl.qfq.close)
        elif pol == WritePolicy.FULL_QFQ:
            bar = pl.qfq or pl.raw
            if bar is not None:
                _apply_bar_full(snap, bar, prev_qfq_close, adj="qfq")
                if pl.qfq is not None and pl.qfq.close:
                    prev_qfq_close = pl.qfq.close
            for k in ("pe", "pb", "market_cap", "turnover"):
                v = pl.extras.get(k)
                if v is not None:
                    setattr(snap, k, v)
        else:  # PATCH_STATUS
            bar = pl.qfq or pl.raw
            if bar is not None:
                _patch_status_only(snap, bar)
            if pl.qfq is not None and pl.qfq.close:
                prev_qfq_close = pl.qfq.close

        if is_new:
            session.add(snap)
            existing[d] = snap
        n += 1

    if skipped_future:
        print(
            f"  [quote_writer] {code}: 丢弃 {skipped_future} 根 quote_date > "
            f"{cap} 的 bar（日期保证）", flush=True,
        )
    return n


# ───────────────────────── etf / index writers ─────────────────────────

def upsert_etf_daily(
    session: Session,
    code: str,
    rows: list[dict],
    *,
    cap_date: date | datetime | str,
) -> int:
    """etf_quote_daily **唯一**写入入口（有则覆盖 OHLC，无则插入）。quote_date > cap_date 跳过。"""
    cap = _coerce_date(cap_date)
    if not rows:
        return 0
    existing = {
        r.quote_date: r for r in session.exec(
            select(EtfQuoteDaily).where(EtfQuoteDaily.asset_code == code)
        ).all()
    }
    n = 0
    skipped = 0
    for r in rows:
        d = r["quote_date"]
        if d > cap:
            skipped += 1
            continue
        row = existing.get(d)
        if row is None:
            session.add(EtfQuoteDaily(**r))
            n += 1
        else:
            for k in ("open", "high", "low", "close", "pct_chg", "volume", "amount"):
                if r.get(k) is not None:
                    setattr(row, k, r[k])
            n += 1
    if skipped:
        print(f"  [quote_writer] {code}: 丢弃 {skipped} 根 > {cap} 的 ETF bar", flush=True)
    return n


def upsert_index_daily(
    session: Session,
    code: str,
    rows: list[dict],
    *,
    cap_date: date | datetime | str,
    overwrite: bool = False,
) -> int:
    """index_quote_daily **唯一**写入入口。overwrite=False(默认)跳过已有日；True 则覆盖 OHLC。

    quote_date > cap_date 一律跳过。
    """
    cap = _coerce_date(cap_date)
    if not rows:
        return 0
    existing = {
        r.quote_date: r for r in session.exec(
            select(IndexQuoteDaily).where(IndexQuoteDaily.asset_code == code)
        ).all()
    }
    n = 0
    skipped = 0
    for r in rows:
        d = r["quote_date"]
        if d > cap:
            skipped += 1
            continue
        row = existing.get(d)
        if row is None:
            session.add(IndexQuoteDaily(**r))
            n += 1
        elif overwrite:
            for k in ("open", "high", "low", "close", "pct_chg", "volume", "amount"):
                if r.get(k) is not None:
                    setattr(row, k, r[k])
            n += 1
    if skipped:
        print(f"  [quote_writer] {code}: 丢弃 {skipped} 根 > {cap} 的 index bar", flush=True)
    return n
