"""数据层门面：按市场在多个数据源间做优先级 fallback。

借鉴 daily_stock_analysis 的 DataFetcherManager：单源失败自动降级，
上层业务（services / api / tui）只与本门面交互，不直接碰具体数据源。
"""
from __future__ import annotations

from typing import Optional

from stockfu.data.base import (DividendMetric, KlineBar, Market, Quote, detect_market)
from stockfu.data.akshare_source import AkshareSource
from stockfu.data.baostock_source import BaostockSource
from stockfu.data.efinance_source import EfinanceSource
from stockfu.data.pytdx_source import PytdxSource
from stockfu.data.sina_source import SinaSource
from stockfu.data.tencent_source import TencentSource
from stockfu.data.yfinance_source import YfinanceSource


class DataProviderManager:
    def __init__(self) -> None:
        self.efinance = EfinanceSource()
        self.tencent = TencentSource()
        self.sina = SinaSource()
        self.pytdx = PytdxSource()
        self.baostock = BaostockSource()
        self.akshare = AkshareSource()
        self.yfinance = YfinanceSource()
        # 行情 / K 线优先级：efinance(东财)→tencent→sina→pytdx→baostock(独立梯队)→akshare→yfinance
        self._quote_order: list = [self.efinance, self.tencent, self.sina, self.pytdx,
                                   self.baostock, self.akshare, self.yfinance]

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
        market = detect_market(code)
        # A 股分红主力 akshare；港美股主力 yfinance
        candidates = ([self.akshare, self.yfinance] if market == Market.CN
                      else [self.yfinance, self.akshare])
        for s in candidates:
            fn = getattr(s, "get_dividend_metric", None)
            if fn is None:
                continue
            try:
                m = fn(code, latest_price=latest_price)
            except Exception:  # noqa: BLE001
                m = None
            if m and m.events:
                return m
        return None

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


_manager: Optional[DataProviderManager] = None


def get_manager() -> DataProviderManager:
    global _manager
    if _manager is None:
        _manager = DataProviderManager()
    return _manager
