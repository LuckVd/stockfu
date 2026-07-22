"""个股 PE/PB 历史分位 + 中枢/目标价(回测与荐股无未来函数用)。

baostock 全字段 backfill 已把 peTTM/pbMRQ 落入 quote_snapshot.pe/pb。
本模块按 as_of 读 quote_snapshot <=as_of 序列，本地、无网络、无未来函数。
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from datetime import date, timedelta
from typing import Any

from sqlmodel import select

from stockfu.db import session_scope
from stockfu.models import QuoteSnapshot
from stockfu.services import factors as F


# 回测估值供给器：engine 预载行情后挂载，避免 value 算子对每个
# (code, as_of) 都开一次 session 查询多年 PE/PB 序列。
# fn(code, start, ref_date) -> list[(date, close, pe, pb)] | None。
# None 表示不在回测预载范围内，必须回退 DB 以保持 live/边界调用正确。
_BT_VALUATION_PROVIDER = None


def set_backtest_valuation_provider(fn) -> None:
    """挂载回测估值内存供给器（由 backtest.engine 生命周期管理）。"""
    global _BT_VALUATION_PROVIDER
    _BT_VALUATION_PROVIDER = fn


def clear_backtest_valuation_provider() -> None:
    """摘除回测估值供给器，恢复 live 路径的数据库读取。"""
    global _BT_VALUATION_PROVIDER
    _BT_VALUATION_PROVIDER = None


def _quantile(sorted_vals: list[float], q: float) -> float | None:
    """线性插值分位,q∈[0,1]。空序列 → None。"""
    if not sorted_vals:
        return None
    n = len(sorted_vals)
    if n == 1:
        return float(sorted_vals[0])
    q = max(0.0, min(1.0, q))
    pos = (n - 1) * q
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return float(sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac)


def _percentile_sorted(sorted_vals: list[float], value: float | None) -> float | None:
    """已排序数列上的平均秩分位，语义与 factors.percentile 一致但不重复排序。"""
    if value is None or len(sorted_vals) < 10:
        return None
    below = bisect_left(sorted_vals, value)
    equal = bisect_right(sorted_vals, value) - below
    return round((below + equal / 2) / len(sorted_vals) * 100, 2)


def _zone_from_pcts(pe_pct: float | None, pb_pct: float | None) -> str:
    """对齐估值顾问阈值: <20 cheap / 20–80 fair / >80 rich; 无样本 unknown。

    双字段取可用值的均值;更偏买入保守——任一 >80 直接 rich。
    """
    vals = [p for p in (pe_pct, pb_pct) if p is not None]
    if not vals:
        return "unknown"
    if any(p > 80 for p in vals):
        return "rich"
    avg = sum(vals) / len(vals)
    if avg < 20:
        return "cheap"
    if avg > 80:
        return "rich"
    return "fair"


def _fair_price(close: float | None, cur_mult: float | None,
                med_mult: float | None) -> float | None:
    """把当前倍数拉回历史中位对应的价格: close * (med / cur)。"""
    if close is None or close <= 0:
        return None
    if cur_mult is None or cur_mult <= 0 or med_mult is None or med_mult <= 0:
        return None
    return round(close * (med_mult / cur_mult), 4)


def valuation_snapshot(
    code: str,
    as_of: date,
    years: int = 5,
    close: float | None = None,
) -> dict[str, Any]:
    """一次扫描产出分位 + 中枢价 + 价格带(严格 <=as_of)。

    返回字段:
      pe, pb, pe_pct, pb_pct, n_pe, n_pb,
      pe_med/p25/p75, pb_med/p25/p75,
      fair_price_pe, fair_price_pb, value_zone, value_band, close
    样本<10 或 pe/pb 无效 → 对应 pct/fair 为 None, value_zone=unknown。
    """
    empty: dict[str, Any] = {
        "pe": None, "pb": None, "pe_pct": None, "pb_pct": None,
        "n_pe": 0, "n_pb": 0,
        "pe_med": None, "pe_p25": None, "pe_p75": None,
        "pb_med": None, "pb_p25": None, "pb_p75": None,
        "fair_price_pe": None, "fair_price_pb": None,
        "value_zone": "unknown", "value_band": None, "close": close,
    }
    start = as_of - timedelta(days=years * 365 + 15)
    rows: list[tuple[date, float | None, float | None, float | None]] | None = None
    if _BT_VALUATION_PROVIDER is not None:
        rows = _BT_VALUATION_PROVIDER(code, start, as_of)
    if rows is None:
        with session_scope() as s:
            db_rows = s.exec(select(QuoteSnapshot).where(
                QuoteSnapshot.asset_code == code,
                QuoteSnapshot.quote_date >= start,
                QuoteSnapshot.quote_date <= as_of,
            ).order_by(QuoteSnapshot.quote_date)).all()
        rows = [
            (r.quote_date, r.close, r.pe, r.pb)
            for r in db_rows
        ]
    if not rows:
        return empty

    _d, row_close, row_pe, row_pb = rows[-1]
    pe = row_pe if row_pe and row_pe > 0 else None
    pb = row_pb if row_pb and row_pb > 0 else None
    if close is None:
        close = row_close if row_close and row_close > 0 else None

    pes = sorted(row_pe for _d, _close, row_pe, _pb in rows if row_pe and row_pe > 0)
    pbs = sorted(row_pb for _d, _close, _pe, row_pb in rows if row_pb and row_pb > 0)

    pe_pct = _percentile_sorted(pes, pe)
    pb_pct = _percentile_sorted(pbs, pb)

    pe_med = _quantile(pes, 0.50) if len(pes) >= 10 else None
    pe_p25 = _quantile(pes, 0.25) if len(pes) >= 10 else None
    pe_p75 = _quantile(pes, 0.75) if len(pes) >= 10 else None
    pb_med = _quantile(pbs, 0.50) if len(pbs) >= 10 else None
    pb_p25 = _quantile(pbs, 0.25) if len(pbs) >= 10 else None
    pb_p75 = _quantile(pbs, 0.75) if len(pbs) >= 10 else None

    fair_pe = _fair_price(close, pe, pe_med)
    fair_pb = _fair_price(close, pb, pb_med)

    # 价格带优先 PE 25–75,否则 PB
    value_band = None
    if close and pe and pe > 0 and pe_p25 and pe_p75:
        lo = round(close * (pe_p25 / pe), 4)
        hi = round(close * (pe_p75 / pe), 4)
        value_band = [min(lo, hi), max(lo, hi)]
    elif close and pb and pb > 0 and pb_p25 and pb_p75:
        lo = round(close * (pb_p25 / pb), 4)
        hi = round(close * (pb_p75 / pb), 4)
        value_band = [min(lo, hi), max(lo, hi)]

    return {
        "pe": pe,
        "pb": pb,
        "pe_pct": pe_pct,
        "pb_pct": pb_pct,
        "n_pe": len(pes),
        "n_pb": len(pbs),
        "pe_med": round(pe_med, 4) if pe_med is not None else None,
        "pe_p25": round(pe_p25, 4) if pe_p25 is not None else None,
        "pe_p75": round(pe_p75, 4) if pe_p75 is not None else None,
        "pb_med": round(pb_med, 4) if pb_med is not None else None,
        "pb_p25": round(pb_p25, 4) if pb_p25 is not None else None,
        "pb_p75": round(pb_p75, 4) if pb_p75 is not None else None,
        "fair_price_pe": fair_pe,
        "fair_price_pb": fair_pb,
        "value_zone": _zone_from_pcts(pe_pct, pb_pct),
        "value_band": value_band,
        "close": close,
    }


def valuation_percentile(code: str, as_of: date, years: int = 5) -> tuple[float | None, float | None]:
    """读 quote_snapshot <=as_of 近 years 年序列，算 as_of 当天 PE/PB 分位(0-100)。

    本地、无网络、无未来函数。样本<10 或无当天值返回 (None, None)。
    兼容旧调用;新代码优先 valuation_snapshot。
    """
    snap = valuation_snapshot(code, as_of, years=years)
    return snap.get("pe_pct"), snap.get("pb_pct")
