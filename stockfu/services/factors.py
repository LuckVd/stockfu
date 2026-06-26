"""因子分位计算工具（CNN 式：每个因子取自身历史分位 → 0-100）。

历史窗口按因子类别（业界口径）：
- 估值类(PE/PB/股息率)：近 10 年或成立来取短（覆盖牛熊周期，对齐 CAPE 标准）
- 情绪/资金/波动/热度类：近 5 年（A 股 10 年间市场扩容 + 量化崛起，
  量价类指标 10 年前后不可比，会失真；5 年覆盖一个 A 股牛熊周期）
样本不足 10 时返回 None 并带 sample_size 供标注。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

from sqlmodel import select

from stockfu.db import session_scope
from stockfu.models import QuoteSnapshot

# 估值类因子 → 长窗口；其余 → 中窗口
VALUATION_FACTORS = {"pe", "pb", "dividend_yield", "erp"}
WINDOW_LONG_DAYS = 365 * 10   # 估值类 ~10 年
WINDOW_MID_DAYS = 365 * 5     # 情绪/资金/波动/热度 ~5 年


def window_days_for(factor: str) -> int:
    return WINDOW_LONG_DAYS if factor in VALUATION_FACTORS else WINDOW_MID_DAYS


def percentile(series: Iterable[float], value: float | None) -> tuple[float | None, int]:
    """value 在 series 中的历史分位(0-100，平均秩法)。样本<10 返回 (None, n)。"""
    if value is None:
        s = sorted(x for x in series if x is not None)
        return None, len(s)
    s = sorted(x for x in series if x is not None)
    n = len(s)
    if n < 10:
        return None, n
    below = sum(1 for x in s if x < value)
    equal = sum(1 for x in s if x == value)
    return round((below + equal / 2) / n * 100, 2), n


def quote_series(code: str, field: str, days: int) -> list[float]:
    """从 quote_snapshot 读某字段近 days 日的序列。"""
    start = date.today() - timedelta(days=days + 15)
    with session_scope() as s:
        rows = s.exec(select(QuoteSnapshot).where(
            QuoteSnapshot.asset_code == code,
            QuoteSnapshot.quote_date >= start,
        ).order_by(QuoteSnapshot.quote_date)).all()
    return [getattr(r, field) for r in rows if getattr(r, field) is not None]


def factor_percentile(code: str, factor: str, field: str,
                      today_value: float | None) -> tuple[float | None, int, int]:
    """算某因子当日值在历史的分位。返回 (分位, 样本数, 窗口天数)。"""
    days = window_days_for(factor)
    series = quote_series(code, field, days)
    pct, n = percentile(series, today_value)
    return pct, n, days
