"""因子分位计算工具（CNN 式：每个因子取自身历史分位 → 0-100）。

percentile() 为通用分位函数(平均秩法,样本<10 返回 None);历史窗口由各调用方
按因子类别自选(composite 用 WINDOW_MID_DAYS 5 年窗口;valuation 读 PE/PB 序列算分位)。

价格复权口径(quote_series adj):
  qfq 前复权 — 回测成交/动量/低波/布林等(默认)
  raw 不复权 — 股息率等名义金额/股价比
  hfq 后复权 — 备用
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

from sqlmodel import select

from stockfu.db import session_scope
from stockfu.models import EtfQuoteDaily, IndexQuoteDaily, QuoteSnapshot

# 情绪/量价类因子历史窗口(composite 三层情绪 + sector_rotation 探针对齐复用)
WINDOW_MID_DAYS = 365 * 5     # ~5 年(覆盖一个 A 股牛熊周期)

# 复权口径(baostock adjustflag: 2=qfq 3=raw 1=hfq)
ADJ_QFQ = "qfq"   # 前复权
ADJ_RAW = "raw"   # 不复权
ADJ_HFQ = "hfq"   # 后复权
VALID_ADJ = frozenset({ADJ_QFQ, ADJ_RAW, ADJ_HFQ})
_OHLC = frozenset({"open", "high", "low", "close"})


def price_column(field: str, adj: str = ADJ_QFQ) -> str:
    """逻辑字段(open/high/low/close) + 复权口径 → 物理列名。

    个股 quote_snapshot: close_qfq / close_raw / close_hfq。
    遗留 close 仅作 qfq 回落(迁移前/未同步时)。
    ETF/指数表无三套列,调用方对非个股应只用 adj=qfq 且读 open/high/low/close。
    """
    f = (field or "close").lower()
    a = (adj or ADJ_QFQ).lower()
    if f not in _OHLC:
        return f  # volume 等非价格字段原样
    if a not in VALID_ADJ:
        raise ValueError(f"unknown adj={adj!r}; expect qfq|raw|hfq")
    return f"{f}_{a}"


def _row_price(row, field: str, adj: str = ADJ_QFQ) -> float | None:
    """从 ORM/行对象取价:显式列优先,qfq 回落遗留 open/high/low/close。"""
    col = price_column(field, adj)
    v = getattr(row, col, None)
    if v is not None:
        return float(v)
    if (adj or ADJ_QFQ).lower() == ADJ_QFQ:
        legacy = getattr(row, field, None)
        if legacy is not None:
            return float(legacy)
    return None


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


# 回测行情供给器：engine 预载全量行情后挂上，让 quote_series 从内存切片(零 DB)。
# fn(code, field, start, ref_date) -> list[float] | None；返回 None 表示未覆盖 → 回落查库。
_BT_SERIES_PROVIDER = None


def set_backtest_series_provider(fn) -> None:
    """挂载回测行情供给器(由 backtest.engine 在预载后调)。"""
    global _BT_SERIES_PROVIDER
    _BT_SERIES_PROVIDER = fn


def clear_backtest_series_provider() -> None:
    """摘除供给器(live / 回测结束)；摘除后 quote_series 走原 DB 路径。"""
    global _BT_SERIES_PROVIDER
    _BT_SERIES_PROVIDER = None


# 带日期的回测行情供给器：供 monthly/weekly_bollinger 等需按日聚合的算子用。
# fn(code, field, start, ref_date) -> (list[date], list[float]) | None；None = 未覆盖回落查库。
_BT_BARS_PROVIDER = None


def set_backtest_bars_provider(fn) -> None:
    """挂载带日期的回测行情供给器(由 backtest.engine 预载后调;与 series 同源内存)。"""
    global _BT_BARS_PROVIDER
    _BT_BARS_PROVIDER = fn


def clear_backtest_bars_provider() -> None:
    """摘除带日期供给器(回测结束)。"""
    global _BT_BARS_PROVIDER
    _BT_BARS_PROVIDER = None


def quote_series(code: str, field: str, days: int, as_of: date | None = None,
                 adj: str = ADJ_QFQ) -> list[float]:
    """从行情表读某字段近 days 日的序列。

    Args:
        code: 股票代码
        field: 字段名 (close/open/high/low)
        days: 回溯天数
        as_of: 基准日期 (默认今天，回测时传入历史日期)
        adj: 复权口径 qfq|raw|hfq(默认前复权)。仅个股 quote_snapshot 有三套;
             ETF/指数无 raw/hfq 列时 raw/hfq 会得到空序列(勿用于成交)。

    Returns:
        价格序列列表(窗口 [ref_date-(days+15), ref_date] 内全部交易日该字段，升序，滤 None)

    回测时若挂了供给器且 adj=qfq 能覆盖窗口 → 从预载内存切片(与下方 DB 路径逐值一致)；
    raw/hfq 不走回测预载(预载仅 qfq 成交价),直接查库。
    """
    ref_date = as_of or date.today()
    start = ref_date - timedelta(days=days + 15)
    adj_n = (adj or ADJ_QFQ).lower()
    # 回测内存供给器只缓存前复权成交价
    if adj_n == ADJ_QFQ and _BT_SERIES_PROVIDER is not None:
        got = _BT_SERIES_PROVIDER(code, field, start, ref_date)
        if got is not None:
            return got
    model = quote_model_for(code)
    with session_scope() as s:
        rows = s.exec(select(model).where(
            model.asset_code == code,
            model.quote_date >= start,
            model.quote_date <= ref_date,  # 关键修复：限制在基准日期之前
        ).order_by(model.quote_date)).all()
    if model is QuoteSnapshot and field.lower() in _OHLC:
        out = []
        for r in rows:
            v = _row_price(r, field, adj_n)
            if v is not None:
                out.append(v)
        return out
    # ETF/指数:仅遗留 open/high/low/close(按 qfq 语义)
    return [getattr(r, field) for r in rows if getattr(r, field) is not None]


def quote_series_dates(code: str, field: str, days: int,
                       as_of: date | None = None,
                       adj: str = ADJ_QFQ) -> tuple[list[date], list[float]]:
    """带日期的 quote_series:返回 (dates, values)(升序、滤 None、逐值对齐)。

    供 monthly/weekly_bollinger 等需按日聚合(月/周最后值)的算子用,避免每个
    (code, as_of) 各自开 session 查库的 N+1。回测时走预载 bars 供给器(零 DB,
    与 quote_series 同源内存);未挂载或非个股非 qfq → 回落单次查库。

    values 与 quote_series(code, field, days, as_of, adj) 逐值一致(同窗口、同过滤、同升序)。
    """
    ref_date = as_of or date.today()
    start = ref_date - timedelta(days=days + 15)
    adj_n = (adj or ADJ_QFQ).lower()
    if adj_n == ADJ_QFQ and _BT_BARS_PROVIDER is not None:
        got = _BT_BARS_PROVIDER(code, field, start, ref_date)
        if got is not None:
            return got
    model = quote_model_for(code)
    with session_scope() as s:
        rows = s.exec(select(model).where(
            model.asset_code == code,
            model.quote_date >= start,
            model.quote_date <= ref_date,
        ).order_by(model.quote_date)).all()
    dates: list[date] = []
    values: list[float] = []
    for r in rows:
        d = getattr(r, "quote_date", None) or getattr(r, "snap_date", None)
        if model is QuoteSnapshot and field.lower() in _OHLC:
            v = _row_price(r, field, adj_n)
        else:
            raw = getattr(r, field, None)
            v = float(raw) if raw is not None else None
        if v is not None and d is not None:
            dates.append(d)
            values.append(v)
    return dates, values


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
