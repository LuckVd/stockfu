"""数据层门面：按市场在多个数据源间做优先级 fallback。

借鉴 daily_stock_analysis 的 DataFetcherManager：单源失败自动降级，
上层业务（services / api）只与本门面交互，不直接碰具体数据源。

硬约束：行情/K 线只接入**前复权(qfq)**源；已移除 sina / pytdx 等不复权源。
"""
from __future__ import annotations

from typing import Optional

from stockfu.data.base import (DividendMetric, KlineBar, Market, Quote, TTLCache, detect_market)
from stockfu.data.akshare_source import AkshareSource
from stockfu.data.baostock_source import BaostockSource
from stockfu.data.efinance_source import EfinanceSource
from stockfu.data.tencent_source import TencentSource
from stockfu.data.yfinance_source import YfinanceSource


class DataProviderManager:
    def __init__(self) -> None:
        self.efinance = EfinanceSource()
        self.tencent = TencentSource()
        self.baostock = BaostockSource()
        self.akshare = AkshareSource()
        self.yfinance = YfinanceSource()
        # 分红数据低频（一年几次），缓存 1 小时，避免每次刷新自选都全量联网拉取
        self._dividend_cache = TTLCache(3600)
        self._index_cache = TTLCache(300)
        # 行情 / K 线优先级（全部前复权）:
        # baostock(adjustflag=2) → efinance(fqt=1) → tencent(qfq) → akshare(qfq) → yfinance(auto_adjust)
        # baostock 无 get_quote(继承 base 返回 None)→ CN 取实时盘自动降级 efinance；港美股被 supports={CN} 跳过
        self._quote_order: list = [
            self.baostock, self.efinance, self.tencent, self.akshare, self.yfinance,
        ]

    def _ordered_for(self, market: str) -> list:
        return [s for s in self._quote_order if market in s.supports]

    # -------- 行情 --------
    def get_quote(self, code: str) -> Optional[Quote]:
        market = detect_market(code)
        for s in self._ordered_for(market):
            try:
                q = s.get_quote(code)
            except Exception:  # noqa: BLE001
                q = None
            if q and q.price and q.price > 0:
                return q
        return None

    # -------- 分红 / 股息率 --------
    def get_dividend_metric(self, code: str,
                            latest_price: Optional[float] = None) -> Optional[DividendMetric]:
        # 分红事件低频，按 code 缓存；并对「查不到」做负缓存——ETF/无分红股反复
        # 触发 akshare→yfinance 联网重试是 watchlist 卡顿的主因。
        cached = self._dividend_cache.get(code)
        if cached is not None:
            return cached if cached.events else None   # 空 marker 命中→None（无分红）
        market = detect_market(code)
        # A 股分红主力 baostock(query_dividend_data 免费稳定,字段结构化)→akshare→yfinance;
        # 港美股主力 yfinance
        candidates = ([self.baostock, self.akshare, self.yfinance] if market == Market.CN
                      else [self.yfinance, self.akshare])
        result: Optional[DividendMetric] = None
        for s in candidates:
            fn = getattr(s, "get_dividend_metric", None)
            if fn is None:
                continue
            try:
                m = fn(code, latest_price=latest_price)
            except Exception:  # noqa: BLE001
                m = None
            if m and m.events:
                result = m
                break
        # 有 events 缓存 metric；否则负缓存空 marker（1h 内不再重试），对外返回 None
        self._dividend_cache.set(code, result if result is not None else DividendMetric(code=code))
        return result

    # -------- K 线 --------
    def get_kline(self, code: str, days: int = 365) -> list[KlineBar]:
        market = detect_market(code)
        for s in self._ordered_for(market):
            try:
                bars = s.get_kline(code, days)
            except Exception:  # noqa: BLE001
                bars = []
            if bars:
                return bars
        return []

    # -------- 资金流 / 板块（akshare） --------
    def get_stock_fund_flow(self, code: str) -> dict:
        return self.akshare.get_stock_fund_flow(code)

    def get_sector_fund_flow(self, top_n: int = 8) -> dict:
        return self.akshare.get_sector_fund_flow(top_n)

    def get_etf_fund_flow(self, code: str) -> dict:
        return self.akshare.get_etf_fund_flow(code)

    # -------- 板块（同花顺：板块K线+成交额 / 板块资金流 / 大盘资金流）--------
    def get_sector_kline(self, sector_name: str, days: int = 1460) -> list:
        return self.akshare.get_sector_kline(sector_name, days)

    def get_sector_flow_today(self) -> list:
        return self.akshare.get_sector_flow_today()

    def get_market_fund_flow(self) -> list:
        return self.akshare.get_market_fund_flow()

    def get_index_quotes(self) -> dict:
        """主要指数实时点数/涨跌幅（akshare 东财指数系列：上证+深证）。
        返回 {code: {name, price, pct_chg}}，含上证指数/创业板指/科创50。"""
        cached = self._index_cache.get("all")
        if cached is not None:
            return cached
        import akshare as ak
        out: dict = {}
        for sym, codes in (("上证系列指数", ("000001", "000688")),
                           ("深证系列指数", ("399006",))):
            try:
                df = ak.stock_zh_index_spot_em(symbol=sym)
            except Exception:  # noqa: BLE001
                continue
            for _, r in df.iterrows():
                c = str(r.get("代码", "")).strip()
                if c in codes:
                    try:
                        price = float(r.get("最新价"))
                        chg = float(r.get("涨跌幅"))
                    except (TypeError, ValueError):
                        continue
                    out[c] = {"name": str(r.get("名称", "")).strip(),
                              "price": price, "pct_chg": chg}
        self._index_cache.set("all", out)
        return out


_manager: Optional[DataProviderManager] = None


def get_manager() -> DataProviderManager:
    global _manager
    if _manager is None:
        _manager = DataProviderManager()
    return _manager
