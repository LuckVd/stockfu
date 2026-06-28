"""akshare 数据源：A 股分红（主力）+ 个股/板块资金流 + ETF 份额 + 实时兜底。

分红：优先 stock_history_dividend_detail（「派息」列为每 10 股口径，每股 = 派息/10，
最稳定可靠）；fhps/cninfo 作文本解析兜底。列名容错借鉴
daily_stock_analysis/data_provider/fundamental_adapter.py (MIT)。
"""
from __future__ import annotations

import time
from datetime import datetime, date, timedelta
from typing import Optional

import pandas as pd

from stockfu.data.base import (DataSource, KlineBar, Market, Quote, currency_of,
                            detect_market, direct_connection)
from stockfu.data.dividend_parser import (_filter_rows as filter_rows,
                                       _pick as pick_col, build_metric_from_df,
                                       build_metric_from_fhps, build_metric_from_history, safe_float,
                                       safe_str)


def _call_df(candidates):
    """逐个尝试 akshare 函数，返回首个非空 DataFrame。借鉴参考的 _call_df_candidates。"""
    errors = []
    with direct_connection():  # akshare 须在无代理环境 import
        try:
            import akshare as ak
        except Exception as exc:  # noqa: BLE001
            return None, None, [f"import:{type(exc).__name__}"]
    for func_name, kwargs in candidates:
        fn = getattr(ak, func_name, None)
        if fn is None:
            continue
        try:
            with direct_connection():
                df = fn(**kwargs)
            if isinstance(df, pd.Series):
                df = df.to_frame().T
            if isinstance(df, pd.DataFrame) and not df.empty:
                return df, func_name, errors
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{func_name}:{type(exc).__name__}")
    return None, None, errors


class AkshareSource(DataSource):
    name = "akshare"
    supports = {Market.CN, Market.HK, Market.US}

    # 全量 A 股实时表（类级缓存 20 分钟，仅实时兜底用）
    _spot_df: Optional[pd.DataFrame] = None
    _spot_ts: float = 0.0
    # 全量港股实时表（类级缓存 20 分钟，仅取名称用）
    _hk_spot_df: Optional[pd.DataFrame] = None
    _hk_spot_ts: float = 0.0

    # -------- 行情（兜底；主力失败时全量 spot 缓存） --------
    def _fetch_quote(self, code: str) -> Optional[Quote]:
        mkt = detect_market(code)
        if mkt == Market.HK:
            return self._fetch_quote_hk(code)
        if mkt == Market.US:
            return self._fetch_quote_us(code)
        now = time.monotonic()
        if self._spot_df is None or now - self._spot_ts > 1200:
            try:
                with direct_connection():  # akshare 须在无代理环境 import+调用
                    import akshare as ak
                    self._spot_df = ak.stock_zh_a_spot_em()
                self._spot_ts = now
            except Exception:  # noqa: BLE001
                return None
        df = self._spot_df
        if df is None or df.empty:
            return None
        code_col = next((c for c in df.columns if "代码" in str(c)), None)
        if code_col is None:
            return None
        row = df[df[code_col].astype(str) == code]
        if row.empty:
            return None
        r = row.iloc[0]

        def f(*keys):
            return safe_float(pick_col(r, list(keys)))

        return Quote(
            code=code, name=safe_str(pick_col(r, ["名称", "股票名称"])),
            market=Market.CN, currency=currency_of(Market.CN),
            price=f("最新价", "现价") or 0.0,
            pct_chg=f("涨跌幅"), open=f("今开"), high=f("最高"), low=f("最低"),
            pre_close=f("昨收"), volume=f("成交量"), amount=f("成交额"),
            pe=f("市盈率", "市盈率-动态"), pb=f("市净率"), market_cap=f("总市值"),
            updated_at=datetime.now(),
        )

    # -------- 港股（stock_hk_hist，国内源直连，无需代理） --------
    def _hk_symbol(self, code: str) -> str:
        """HK00700 → 00700（akshare stock_hk_hist 的 symbol）。"""
        return code[2:] if code.startswith("HK") else code

    def _hk_name(self, code: str) -> str:
        """港股名称（stock_hk_spot_em 全量缓存 20 分钟）。"""
        sym = self._hk_symbol(code)
        now = time.monotonic()
        if self._hk_spot_df is None or now - self._hk_spot_ts > 1200:
            try:
                with direct_connection():
                    import akshare as ak
                    self._hk_spot_df = ak.stock_hk_spot_em()
                self._hk_spot_ts = now
            except Exception:  # noqa: BLE001
                return ""
        df = self._hk_spot_df
        if df is None or df.empty:
            return ""
        code_col = next((c for c in df.columns if "代码" in str(c)), None)
        if code_col is None:
            return ""
        row = df[df[code_col].astype(str) == sym]
        return safe_str(pick_col(row.iloc[0], ["名称"])) if not row.empty else ""

    def _hk_bars(self, code: str, days: int) -> list:
        sym = self._hk_symbol(code)
        end = date.today().strftime("%Y%m%d")
        start = (date.today() - timedelta(days=days + 30)).strftime("%Y%m%d")
        df, _, _ = _call_df([("stock_hk_hist", {
            "symbol": sym, "period": "daily", "adjust": "qfq",
            "start_date": start, "end_date": end,
        })])
        if df is None:
            return []
        bars: list = []
        for _, r in df.iterrows():
            try:
                d = pd.to_datetime(r["日期"]).date()
                bars.append(KlineBar(
                    date=d,
                    open=float(r["开盘"]), close=float(r["收盘"]),
                    high=float(r["最高"]), low=float(r["最低"]),
                    volume=float(r["成交量"]) if pd.notna(r.get("成交量")) else None,
                ))
            except (KeyError, ValueError, TypeError):
                continue
        return bars

    def get_kline(self, code: str, days: int = 365) -> list:
        """港股/美股日K（akshare 东财源）；A 股 K 线由 efinance 负责，返回空。"""
        mkt = detect_market(code)
        if mkt == Market.HK:
            bars = self._hk_bars(code, days)
        elif mkt == Market.US:
            bars = self._us_bars(code, days)
        else:
            return []
        return bars[-days:] if days and len(bars) > days else bars

    def _fetch_quote_hk(self, code: str) -> Optional[Quote]:
        """港股现价 = 最近交易日收盘（天级，无盘中实时）。"""
        bars = self._hk_bars(code, 5)
        if not bars:
            return None
        last = bars[-1]
        prev = bars[-2].close if len(bars) >= 2 else last.close
        return Quote(
            code=code, name=self._hk_name(code), market=Market.HK, currency=currency_of(Market.HK),
            price=last.close, open=last.open, high=last.high, low=last.low,
            pre_close=prev, volume=last.volume,
            pct_chg=(((last.close - prev) / prev * 100) if prev else None),
            updated_at=datetime.now(),
        )

    # -------- 美股（stock_us_hist，国内源直连，无需代理） --------
    def _us_bars(self, code: str, days: int) -> list:
        end = date.today().strftime("%Y%m%d")
        start = (date.today() - timedelta(days=days + 30)).strftime("%Y%m%d")
        df = None
        for prefix in ("105", "106"):   # 纳斯达克 / 纽交所，逐个试
            df, _, _ = _call_df([("stock_us_hist", {
                "symbol": f"{prefix}.{code}", "period": "daily", "adjust": "qfq",
                "start_date": start, "end_date": end,
            })])
            if df is not None:
                break
        if df is None:
            return []
        bars: list = []
        for _, r in df.iterrows():
            try:
                d = pd.to_datetime(r["日期"]).date()
                bars.append(KlineBar(
                    date=d,
                    open=float(r["开盘"]), close=float(r["收盘"]),
                    high=float(r["最高"]), low=float(r["最低"]),
                    volume=float(r["成交量"]) if pd.notna(r.get("成交量")) else None,
                ))
            except (KeyError, ValueError, TypeError):
                continue
        return bars

    def _fetch_quote_us(self, code: str) -> Optional[Quote]:
        """美股现价 = 最近交易日收盘（天级）。"""
        bars = self._us_bars(code, 5)
        if not bars:
            return None
        last = bars[-1]
        prev = bars[-2].close if len(bars) >= 2 else last.close
        return Quote(
            code=code, name="", market=Market.US, currency=currency_of(Market.US),
            price=last.close, open=last.open, high=last.high, low=last.low,
            pre_close=prev, volume=last.volume,
            pct_chg=(((last.close - prev) / prev * 100) if prev else None),
            updated_at=datetime.now(),
        )

    # -------- 分红（主力） --------
    def get_dividend_metric(self, code: str, latest_price: Optional[float] = None,
                            currency: Optional[str] = None):
        if detect_market(code) != Market.CN:
            return None
        cur = currency or currency_of(Market.CN)
        # stock_fhps_detail_em 有「报告期」(财年)，按财年累加避免跨财年，优先
        df, used, _ = _call_df([
            ("stock_fhps_detail_em", {"symbol": code}),
            ("stock_history_dividend_detail",
             {"symbol": code, "indicator": "分红", "date": ""}),
            ("stock_dividend_cninfo", {"symbol": code}),
        ])
        if df is None:
            return None
        if "报告期" in df.columns:
            return build_metric_from_fhps(
                df, code, cur, latest_price, source=f"akshare:{used}")
        if "派息" in df.columns:
            return build_metric_from_history(
                df, code, cur, latest_price, source=f"akshare:{used}")
        return build_metric_from_df(
            df, code, currency=cur, latest_price=latest_price, source=f"akshare:{used}")

    def get_dividends(self, code: str, years: int = 5):
        m = self.get_dividend_metric(code)
        return m.events if m else []

    # -------- 资金流（大资金/板块情绪） --------
    def get_stock_fund_flow(self, code: str) -> dict:
        df, used, _ = _call_df([
            ("stock_individual_fund_flow", {"stock": code}),
            ("stock_individual_fund_flow", {"symbol": code}),
        ])
        if df is None:
            return {}
        work = filter_rows(df, code)
        r = (work.iloc[0] if not work.empty
             else (df.iloc[0] if not df.empty else None))
        if r is None:
            return {}
        return {
            "main_net_inflow": safe_float(pick_col(r, ["主力净流入", "主力净流入-净额", "净额"])),
            "super_large": safe_float(pick_col(r, ["超大单净流入", "超大单净流入-净额"])),
            "large": safe_float(pick_col(r, ["大单净流入", "大单净流入-净额"])),
            "source": f"akshare:{used}",
        }

    def get_sector_fund_flow(self, top_n: int = 8) -> dict:
        df, used, _ = _call_df([
            ("stock_sector_fund_flow_rank",
             {"indicator": "今日", "sector_type": "行业资金流"}),
            ("stock_sector_fund_flow_rank", {"indicator": "今日"}),
            ("stock_sector_fund_flow_rank", {}),
        ])
        if df is None:
            return {"top": [], "bottom": [], "source": ""}
        name_col = next((c for c in df.columns
                         if any(k in str(c) for k in ("名称", "行业", "板块"))), None)
        flow_col = (next((c for c in df.columns
                          if "主力净流入" in str(c) and "净额" in str(c)), None)
                    or next((c for c in df.columns if "主力净流入" in str(c)), None))
        if not name_col or not flow_col:
            return {"top": [], "bottom": [], "source": f"akshare:{used}"}
        w = df[[name_col, flow_col]].copy()
        w[flow_col] = pd.to_numeric(w[flow_col], errors="coerce")
        w = w.dropna(subset=[flow_col])
        top, bot = w.nlargest(top_n, flow_col), w.nsmallest(top_n, flow_col)
        return {
            "top": [{"name": safe_str(r[name_col]), "net": float(r[flow_col])}
                    for _, r in top.iterrows()],
            "bottom": [{"name": safe_str(r[name_col]), "net": float(r[flow_col])}
                       for _, r in bot.iterrows()],
            "source": f"akshare:{used}",
        }

    def get_etf_fund_flow(self, code: str) -> dict:
        """尝试取 ETF 净值/份额（大资金追踪）；容错，失败返回 {}。"""
        df, used, _ = _call_df([
            ("fund_etf_fund_daily_em", {}),
            ("fund_etf_spot_em", {}),
        ])
        if df is None:
            return {}
        code_col = next((c for c in df.columns if "代码" in str(c)), None)
        if code_col is None:
            return {}
        row = df[df[code_col].astype(str) == code]
        if row.empty:
            return {}
        r = row.iloc[0]
        return {
            "nav": safe_float(pick_col(r, ["单位净值", "最新价", "现价", "收盘价"])),
            "amount": safe_float(pick_col(r, ["成交额"])),
            "shares": safe_float(pick_col(r, ["基金份额", "份额"])),
            "source": f"akshare:{used}",
        }
