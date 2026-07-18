"""因子分位计算工具（CNN 式：每个因子取自身历史分位 → 0-100）。

percentile() 为通用分位函数(平均秩法,样本<10 返回 None);历史窗口由各调用方
按因子类别自选(composite 用 WINDOW_MID_DAYS 5 年窗口;valuation 读 PE/PB 序列算分位)。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

from sqlmodel import select

from stockfu.db import session_scope
from stockfu.models import EtfQuoteDaily, IndexQuoteDaily, QuoteSnapshot

# 情绪/量价类因子历史窗口(composite 三层情绪 + sector_rotation 探针对齐复用)
WINDOW_MID_DAYS = 365 * 5     # ~5 年(覆盖一个 A 股牛熊周期)


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


def quote_model_for(code: str):
    """按 code 路由行情表(行情已拆表:个股/ETF/指数三表分离)。

    指数(sh/sz 前缀)→ IndexQuoteDaily;ETF(15/50/51/52/56/58 开头)→ EtfQuoteDaily;
    其余(个股 00/30/60/68、北交所、港美股等)→ QuoteSnapshot。调用方零改动。
    """
    if code.startswith(("sh", "sz")):
        return IndexQuoteDaily
    if code[:2] in {"15", "50", "51", "52", "56", "58"}:
        return EtfQuoteDaily
    return QuoteSnapshot


def quote_series(code: str, field: str, days: int, as_of: date | None = None) -> list[float]:
    """从 quote_snapshot 读某字段近 days 日的序列。

    Args:
        code: 股票代码
        field: 字段名 (close/open/high/low)
        days: 回溯天数
        as_of: 基准日期 (默认今天，回测时传入历史日期)

    Returns:
        价格序列列表
    """
    ref_date = as_of or date.today()
    start = ref_date - timedelta(days=days + 15)
    model = quote_model_for(code)
    with session_scope() as s:
        rows = s.exec(select(model).where(
            model.asset_code == code,
            model.quote_date >= start,
            model.quote_date <= ref_date,  # 关键修复：限制在基准日期之前
        ).order_by(model.quote_date)).all()
    return [getattr(r, field) for r in rows if getattr(r, field) is not None]


def ma_alignment(code: str, lookback: int = 250, as_of: date | None = None) -> str | None:
    """MA5/10/20 排列多头/空头/中性。

    返回 "bullish"(MA5>MA10>MA20) / "bearish"(逆序) / "neutral"(交叉/无序) / None(样本<20 日)。"""
    closes = quote_series(code, "close", lookback, as_of=as_of)
    if len(closes) < 20:
        return None
    ma5 = sum(closes[-5:]) / 5
    ma10 = sum(closes[-10:]) / 10
    ma20 = sum(closes[-20:]) / 20
    if ma5 > ma10 > ma20:
        return "bullish"
    if ma5 < ma10 < ma20:
        return "bearish"
    return "neutral"


def linreg_r2(series: list[float]) -> tuple[float, float]:
    """价格序列对时间索引(0,1,...,n-1)的线性回归,返回 (r², slope)。

    r²→1 = 价格沿直线平稳演进(趋势线性度高/平稳);r²→0 = 散乱震荡。
    slope 正负区分方向。供 trend_linearity 等算子复用(衡量"涨得稳不稳")。
    纯 Python(项目无 numpy/pandas 依赖)。样本<3 或方差为 0(价格恒定)→ (0.0, 0.0)。
    """
    n = len(series)
    if n < 3:
        return 0.0, 0.0
    x_mean = (n - 1) / 2.0
    y_mean = sum(series) / n
    sxx = sum((i - x_mean) ** 2 for i in range(n))
    syy = sum((y - y_mean) ** 2 for y in series)
    sxy = sum((i - x_mean) * (y - y_mean) for i, y in enumerate(series))
    if sxx == 0 or syy == 0:
        return 0.0, 0.0
    slope = sxy / sxx
    r = sxy / (sxx * syy) ** 0.5
    r2 = min(1.0, r * r)   # 浮点误差可能使 |r| 微超 1,夹一下
    return round(r2, 6), round(slope, 6)
