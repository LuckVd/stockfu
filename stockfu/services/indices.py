"""自定义指数：恐慌指数 + 热度指数。

基于沪深300ETF(510300)的历史 quote_snapshot 计算（天级序列）。
- 恐慌：近20日年化波动率加权 近5日跌幅 → 0~100（越大越恐慌）
- 热度：近5日均成交额 / 近60日均成交额 × 50 → 50 为平量，>50 放量，<50 缩量

口径是 MVP 近似（中国无公开免费 VIX），首次需先 backfill 60+ 日历史。
"""
from __future__ import annotations

import math
from datetime import date, timedelta

from sqlmodel import select

from stockfu.db import session_scope
from stockfu.models import IndexSnapshot, QuoteSnapshot

BENCHMARK = "510300"  # 沪深300ETF 作为市场基准


def _series(code: str, days: int, field: str = "close") -> list[float]:
    start = date.today() - timedelta(days=days + 15)
    with session_scope() as s:
        rows = s.exec(select(QuoteSnapshot).where(
            QuoteSnapshot.asset_code == code,
            QuoteSnapshot.quote_date >= start,
        ).order_by(QuoteSnapshot.quote_date)).all()
    return [v for r in rows if (v := getattr(r, field)) is not None]


def compute_fear() -> float | None:
    closes = _series(BENCHMARK, 25, "close")
    if len(closes) < 21:
        return None
    rets = [math.log(closes[i] / closes[i - 1])
            for i in range(1, len(closes)) if closes[i - 1] > 0]
    recent = rets[-20:] if len(rets) >= 20 else rets
    mean = sum(recent) / len(recent)
    vol = math.sqrt(sum((r - mean) ** 2 for r in recent) / len(recent)) * math.sqrt(252)
    drop5 = closes[-1] / closes[-6] - 1 if len(closes) >= 6 else 0.0
    fear = vol * 100 * 1.3 + max(-drop5, 0.0) * 200  # 高波+下跌 推高恐慌
    return round(min(100.0, max(0.0, fear)), 2)


def compute_heat() -> float | None:
    amounts = _series(BENCHMARK, 120, "amount")
    if len(amounts) < 25:
        return None
    a5 = sum(amounts[-5:]) / 5
    base_n = min(60, len(amounts) - 5)
    if base_n < 20:
        return None
    base = sum(amounts[-(base_n + 5):-5]) / base_n  # 排除最近5日的历史均量
    return round(a5 / base * 50, 2) if base > 0 else None


def compute_and_save() -> dict:
    fear = compute_fear()
    heat = compute_heat()
    today = date.today()
    saved: dict[str, float] = {}
    with session_scope() as s:
        for key, val in (("fear", fear), ("heat", heat)):
            if val is None:
                continue
            ex = s.exec(select(IndexSnapshot).where(
                IndexSnapshot.index_key == key,
                IndexSnapshot.snap_date == today)).first()
            snap = ex or IndexSnapshot(index_key=key, snap_date=today)
            snap.value = float(val)
            snap.note = {"fear": "恐慌指数", "heat": "热度指数"}[key]
            if snap.id is None:
                s.add(snap)
            saved[key] = val
        s.commit()
    return saved


def latest(n: int = 30) -> dict:
    """读取 fear/heat 最近 n 日序列（供展示/趋势）。"""
    out: dict[str, list] = {}
    with session_scope() as s:
        for key in ("fear", "heat"):
            rows = s.exec(select(IndexSnapshot).where(
                IndexSnapshot.index_key == key
            ).order_by(IndexSnapshot.snap_date.desc()).limit(n)).all()
            out[key] = [{"date": r.snap_date.isoformat(), "value": r.value}
                        for r in reversed(rows)]
    return out
