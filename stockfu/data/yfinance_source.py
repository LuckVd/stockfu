"""yfinance 数据源：港/美/日/韩/台 股行情 + 分红(dividends) + 日K，兼 A 股兜底。

yfinance 的 ticker.dividends 给出每次派息(每股)，是港美股分红历史的主要来源。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd

from stockfu.data.base import (DataSource, DividendEventDTO, DividendMetric,
                            KlineBar, Market, Quote, currency_of, detect_market)


class YfinanceSource(DataSource):
    name = "yfinance"
    supports = {Market.HK, Market.US, Market.JP, Market.KR, Market.TW, Market.CN}

    @staticmethod
    def _yf_symbol(code: str, market: str) -> str:
        if market == Market.CN and code.isdigit() and len(code) == 6:
            return code + (".SS" if code[0] in ("6", "9", "5") else ".SZ")
        if market == Market.HK:
            body = code[2:] if code.startswith("HK") else code
            try:
                return f"{int(body):04d}.HK"  # yfinance 港股需 4 位补零：00700 -> 0700.HK
            except ValueError:
                return body + ".HK"
        return code  # 美股 / 日韩台(已带后缀) 原样

    @staticmethod
    def _proxy_session():
        """带代理的 requests.Session，供 yfinance 访问港/美/日韩台股。

        代理地址来自 web 设置面板（get_overseas_proxy，运行时可变）；setup_network
        不设全局代理(会误伤国内源)，故 yfinance 在此显式注入。
        """
        import requests
        from stockfu.config import get_overseas_proxy
        proxy = get_overseas_proxy()
        s = requests.Session()
        if proxy:
            s.proxies = {"http": proxy, "https": proxy}
        return s

    # -------- 行情 --------
    def _fetch_quote(self, code: str) -> Optional[Quote]:
        """天级收盘价：用最近日K收盘，不抓盘中实时（「只要天级收盘」定位）。"""
        import yfinance as yf

        market = detect_market(code)
        sym = self._yf_symbol(code, market)
        t = yf.Ticker(sym, session=self._proxy_session())
        try:
            h = t.history(period="5d")
        except Exception:  # noqa: BLE001
            h = None
        if h is None or len(h) == 0:
            return None
        last = h.iloc[-1]
        price = float(last["Close"])
        if not price or price <= 0:
            return None
        prev = float(h["Close"].iloc[-2]) if len(h) >= 2 else price

        # 名称/币种尽量取(轻量)，失败留空/兜底——不作为价格来源
        name, cur = "", currency_of(market)
        try:
            info = t.info or {}
            name = info.get("shortName") or info.get("longName") or ""
            cur = info.get("currency") or cur
        except Exception:  # noqa: BLE001
            pass

        return Quote(
            code=code, name=name, market=market, currency=cur, price=price,
            pct_chg=((price - prev) / prev * 100) if prev else None,
            open=float(last["Open"]), high=float(last["High"]),
            low=float(last["Low"]), pre_close=prev,
            volume=float(last.get("Volume") or 0),
            updated_at=datetime.now(),
        )

    # -------- 分红 --------
    def get_dividend_metric(self, code: str,
                            latest_price: Optional[float] = None) -> Optional[DividendMetric]:
        import yfinance as yf

        market = detect_market(code)
        sym = self._yf_symbol(code, market)
        try:
            div = yf.Ticker(sym, session=self._proxy_session()).dividends
        except Exception:  # noqa: BLE001
            div = None
        if div is None or len(div) == 0:
            return None

        cur = currency_of(market)
        today = date.today()
        ttm_start = today - timedelta(days=365)
        events: list[DividendEventDTO] = []
        for dt, v in div.items():
            try:
                d = pd.to_datetime(dt).date()
            except Exception:  # noqa: BLE001
                continue
            if v and float(v) > 0 and d <= today:
                events.append(DividendEventDTO(
                    ex_date=d, per_share_cash=round(float(v), 6),
                    currency=cur, source="yfinance",
                ))
        if not events:
            return None
        events.sort(key=lambda e: e.ex_date, reverse=True)
        ttm = round(sum(e.per_share_cash for e in events
                        if ttm_start <= e.ex_date <= today), 6)
        yp = round(ttm / latest_price * 100, 4) if (latest_price and latest_price > 0) else None
        return DividendMetric(
            code=code, currency=cur, ttm_cash_per_share=ttm,
            ttm_yield_pct=yp, events=events[:8], coverage="yfinance_dividends",
        )

    def get_dividends(self, code: str, years: int = 5):
        m = self.get_dividend_metric(code)
        return m.events if m else []

    # -------- K 线 --------
    def get_kline(self, code: str, days: int = 365) -> list[KlineBar]:
        import yfinance as yf

        market = detect_market(code)
        sym = self._yf_symbol(code, market)
        try:
            period = "max" if days >= 365 * 5 else f"{max(1, days // 365 + 1)}y"
            h = yf.Ticker(sym, session=self._proxy_session()).history(period=period)
        except Exception:  # noqa: BLE001
            return []
        bars: list[KlineBar] = []
        for dt, r in h.tail(days).iterrows():
            try:
                d = pd.to_datetime(dt).date()
            except Exception:  # noqa: BLE001
                continue
            bars.append(KlineBar(
                date=d, open=float(r["Open"]), high=float(r["High"]),
                low=float(r["Low"]), close=float(r["Close"]),
                volume=float(r.get("Volume") or 0),
            ))
        return bars
