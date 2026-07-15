"""数据层基础设施：市场识别、代码标准化、熔断器、TTL 缓存、统一数据结构、数据源基类。

设计直接借鉴 daily_stock_analysis/data_provider/base.py：
- 多数据源各自实现统一接口，由 manager 做优先级 fallback；
- 熔断器：单源连续失败 N 次后进入冷却，避免持续打一个挂掉的源；
- TTL 缓存 + tenacity 重试，降低对外部源的冲击；
- 统一字段名，上层业务不关心数据来自哪个源。
"""
from __future__ import annotations

import contextlib
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import wraps

# ----------------------------- 领域枚举 -----------------------------


class Market:
    CN = "cn"
    HK = "hk"
    US = "us"
    JP = "jp"
    KR = "kr"
    TW = "tw"


class AssetType:
    STOCK = "stock"
    FUND_ETF = "fund_etf"
    FUND_OTC = "fund_otc"
    INDEX = "index"
    BOND = "bond"


_CURRENCY = {
    Market.CN: "CNY", Market.HK: "HKD", Market.US: "USD",
    Market.JP: "JPY", Market.KR: "KRW", Market.TW: "TWD",
}


def currency_of(market: str) -> str:
    return _CURRENCY.get(market, "CNY")


@contextlib.contextmanager
def direct_connection():
    """临时摘除代理环境变量，供国内源(efinance/akshare)直连东财/新浪等。

    这些源底层 session 不读 NO_PROXY——只要进程里有 HTTP_PROXY 就走代理，
    访问国内接口必 SSL 失败(海外代理节点被东财重置)。国内源本应直连，
    故调用时临时清除代理，退出恢复。yfinance 仍走全局代理(港美股需要)。
    """
    keys = ("http_proxy", "https_proxy", "all_proxy",
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
    saved = {k: os.environ.pop(k) for k in keys if k in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)


# ----------------------- 市场识别与代码标准化 -----------------------
# 借鉴 daily_stock_analysis base.py:153-262

_SUFFIX_MARKET = {
    ".SH": Market.CN, ".SZ": Market.CN, ".BJ": Market.CN,
    ".HK": Market.HK,
    ".T": Market.JP, ".KS": Market.KR, ".KQ": Market.KR,
    ".TW": Market.TW, ".TWO": Market.TW,
}


def normalize_stock_code(code: str) -> str:
    """把各种写法标准化为内部唯一 code。

    A股: SH600519 / 600519.SH / sz000001 → 600519
    港股: hk00700 / 00700.HK / 00700   → HK00700
    美股: aapl / AAPL                  → AAPL
    日韩台: 7203.T / 005930.KS         → 7203.T / 005930.KS
    """
    c = re.sub(r"\s+", "", (code or "").strip().upper())
    if not c:
        return c
    # 1) 已知交易所后缀
    for suf, mkt in _SUFFIX_MARKET.items():
        if c.endswith(suf):
            body = c[: -len(suf)]
            if mkt == Market.CN and body.isdigit():
                return body
            if mkt == Market.HK:
                return "HK" + body.zfill(5)
            return c  # 日/韩/台 保留后缀
    # 2) 字母前缀 SH/SZ/BJ/HK/US
    m = re.match(r"^(SH|SZ|BJ|HK|US)([0-9A-Z]+)$", c)
    if m:
        pref, body = m.group(1), m.group(2)
        if pref in ("SH", "SZ", "BJ"):
            return body
        if pref == "HK":
            return "HK" + body.zfill(5)
        return body
    # 3) 纯字母(可含一个点，如 BRK.B) → 美股
    if re.fullmatch(r"[A-Z]+(\.[A-Z]+)?", c):
        return c
    # 4) 纯数字：6 位 → A股；其余 → 港股补零
    if c.isdigit():
        return c if len(c) == 6 else "HK" + c.zfill(5)
    return c


def detect_market(code: str) -> str:
    """根据标准化 code 判定市场。"""
    c = normalize_stock_code(code)
    if c.startswith("HK"):
        return Market.HK
    if re.search(r"\.(T|KS|KQ|TW|TWO)$", c):
        tag = c.rsplit(".", 1)[1]
        return {".T": Market.JP, "T": Market.JP, "KS": Market.KR,
                "KQ": Market.KR, "TW": Market.TW, "TWO": Market.TW}[tag]
    if c.isdigit() and len(c) == 6:
        return Market.CN
    if re.fullmatch(r"[A-Z]+(\.[A-Z]+)?", c):
        return Market.US
    return Market.CN


def classify_asset_type(code: str, market: str) -> str:
    """A股场内基金/ETF 代码段识别；其余默认股票。"""
    if market == Market.CN and code.isdigit() and len(code) == 6:
        # 场内 ETF/基金常见代码段
        if code[:2] in {"51", "50", "52", "56", "58", "15", "16"}:
            return AssetType.FUND_ETF
        return AssetType.STOCK
    return AssetType.STOCK


# ----------------------------- 熔断器 -----------------------------


class CircuitBreaker:
    """CLOSED → (连续失败达阈值) → OPEN(冷却) → HALF_OPEN(放行一次试探) → CLOSED/OPEN。"""

    CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

    def __init__(self, failure_threshold: int = 3, cooldown_seconds: float = 300.0):
        self.failure_threshold = failure_threshold
        self.cooldown = cooldown_seconds
        self._failures = 0
        self._state = self.CLOSED
        self._opened_at = 0.0

    def allow(self) -> bool:
        if self._state == self.OPEN:
            if time.monotonic() - self._opened_at >= self.cooldown:
                self._state = self.HALF_OPEN
                return True
            return False
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._state = self.CLOSED

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._state = self.OPEN
            self._opened_at = time.monotonic()

    @property
    def state(self) -> str:
        return self._state


# ----------------------------- TTL 缓存 -----------------------------


class TTLCache:
    def __init__(self, ttl: float = 1200.0):
        self.ttl = ttl
        self._store: dict[str, tuple[object, float]] = {}

    def get(self, key: str):
        item = self._store.get(key)
        if not item:
            return None
        val, ts = item
        if time.monotonic() - ts > self.ttl:
            self._store.pop(key, None)
            return None
        return val

    def set(self, key: str, val) -> None:
        self._store[key] = (val, time.monotonic())

    def clear(self) -> None:
        self._store.clear()


# --------------------------- 统一数据结构 ---------------------------


@dataclass
class Quote:
    code: str
    name: str = ""
    market: str = Market.CN
    currency: str = "CNY"
    price: float = 0.0
    pct_chg: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    pre_close: float | None = None
    volume: float | None = None
    amount: float | None = None
    pe: float | None = None
    pb: float | None = None
    market_cap: float | None = None
    updated_at: datetime | None = None


@dataclass
class DividendEventDTO:
    ex_date: date
    per_share_cash: float = 0.0
    record_date: date | None = None
    announce_date: date | None = None
    currency: str = "CNY"
    source: str = ""


@dataclass
class DividendMetric:
    """股息指标（核心：TTM 近 12 个月口径）。"""
    code: str
    currency: str = "CNY"
    ttm_cash_per_share: float = 0.0
    ttm_yield_pct: float | None = None
    events: list[DividendEventDTO] = field(default_factory=list)
    coverage: str = ""

    @property
    def annual_cash_per_share(self) -> float:
        """简单按年聚合最近完整一年的每股派息（估算年红利）。"""
        if not self.events:
            return 0.0
        by_year: dict[int, float] = {}
        for e in self.events:
            by_year[e.ex_date.year] = by_year.get(e.ex_date.year, 0.0) + e.per_share_cash
        return max(by_year.values()) if by_year else 0.0


@dataclass
class KlineBar:
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    amount: float | None = None
    # 日状态(baostock 可提供;其它源多为 None——入库保留 NULL,不假装 0/1)
    trade_status: int | None = None   # 1=交易 0=停牌
    is_st: int | None = None          # 1=ST 0=正常


# --------------------------- 数据源基类 ---------------------------


def make_retry():
    """统一的 tenacity 重试装饰器（网络类异常指数退避，最多 3 次）。"""
    from tenacity import (retry, stop_after_attempt, wait_exponential,
                          retry_if_exception_type)

    return retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((ConnectionError, TimeoutError, OSError)),
        reraise=True,
    )


class DataSource:
    """所有数据源的统一接口。子类实现具体抓取，基类提供熔断 + 缓存护栏。"""

    name: str = "base"
    supports: set[str] = set()  # 支持的市场集合，如 {"cn", "hk"}

    def __init__(self):
        self.breaker = CircuitBreaker()
        self._quote_cache = TTLCache(1200)

    def supports_market(self, market: str) -> bool:
        return market in self.supports

    def get_quote(self, code: str) -> Quote | None:
        """对外接口 = 带熔断 + 缓存的模板；子类实现 _fetch_quote。"""
        return self.get_quote_cached(code)

    def get_dividends(self, code: str, years: int = 5) -> list[DividendEventDTO]:
        return []

    def get_kline(self, code: str, days: int = 365) -> list[KlineBar]:
        return []

    def get_quote_cached(self, code: str) -> Quote | None:
        """带熔断 + 缓存的 get_quote 模板。子类实现 _fetch_quote。"""
        cached = self._quote_cache.get(code)
        if cached is not None:
            return cached
        if not self.breaker.allow():
            return None
        try:
            q = self._fetch_quote(code)
        except Exception:
            self.breaker.record_failure()
            return None
        if q is None:
            self.breaker.record_failure()
            return None
        self.breaker.record_success()
        self._quote_cache.set(code, q)
        return q

    def _fetch_quote(self, code: str) -> Quote | None:
        raise NotImplementedError
