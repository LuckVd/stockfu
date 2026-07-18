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


def _find_col(cols, *keys, exclude=()):
    """列名同时包含所有 keys（且不含任何 exclude 词）的第一列；找不到返回 None。

    用于在 akshare 资金流 df 里精确区分同源易混列：如「主力净流入-净额」vs
    「主力净流入-净占比」、「大单净流入-净额」vs「超大单净流入-净额」（后者用 exclude=('超大',)）。
    """
    for c in cols:
        cs = str(c)
        if all(k in cs for k in keys) and not any(x in cs for x in exclude):
            return c
    return None


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


def get_index_daily(symbol: str, start: str, end: str) -> list[dict]:
    """拉取指数日线行情（akshare 优先，baostock 兜底）。

    多源 fallback：先 akshare index_zh_a_hist；akshare 未装/失败/空 → 降级 baostock
    （baostock 已装、支持指数日线 sh.000001 等）。symbol: 指数代码，如 "000001"（上证综指）。
    返回 list[dict]，每 dict 含 asset_code/quote_date/open/high/low/close/pct_chg/volume/amount。
    两者都失败 → 返回 []（调用方据此判定本次未更新）。

    列名映射：000001 → asset_code="sh000001"（StockFu 代码规范）。
    """
    rows = _get_index_daily_akshare(symbol, start, end)
    if rows:
        return rows
    from stockfu.data.baostock_source import get_index_daily_baostock
    rows = get_index_daily_baostock(symbol, start, end)
    if rows:
        return rows
    return _get_index_daily_sina(symbol, start, end)   # 新浪兜底(科创50 000688 等)


def _get_index_daily_akshare(symbol: str, start: str, end: str) -> list[dict]:
    """akshare 指数日线（index_zh_a_hist）；未装/失败/空 → 返回 []。"""
    with direct_connection():
        try:
            import akshare as ak
        except Exception:
            return []
        try:
            df = ak.index_zh_a_hist(symbol=symbol, period="daily",
                                     start_date=start, end_date=end)
        except Exception:
            return []
    if df is None or df.empty:
        return []
    asset_code = f"sh{symbol}"  # 000001 → sh000001
    results: list[dict] = []
    for _, r in df.iterrows():
        try:
            d = pd.to_datetime(r["日期"]).date()
            close_val = float(r["收盘"])
            results.append({
                "asset_code": asset_code,
                "quote_date": d,
                "open": float(r["开盘"]) if pd.notna(r.get("开盘")) else None,
                "high": float(r["最高"]) if pd.notna(r.get("最高")) else None,
                "low": float(r["最低"]) if pd.notna(r.get("最低")) else None,
                "close": close_val,
                "pct_chg": float(r["涨跌幅"]) if pd.notna(r.get("涨跌幅")) else None,
                "volume": float(r["成交量"]) if pd.notna(r.get("成交量")) else None,
                "amount": float(r["成交额"]) if pd.notna(r.get("成交额")) else None,
            })
        except (KeyError, ValueError, TypeError):
            continue
    return results


def _get_index_daily_sina(symbol: str, start: str, end: str) -> list[dict]:
    """akshare 新浪指数日线(stock_zh_index_daily):东财/baostock 无数据时的末级兜底。

    东财 index_zh_a_hist 对部分指数(如科创50 000688)无历史、baostock 亦无,但新浪有。
    新浪返回全历史(无 start/end),按 [start,end] 过滤;无涨跌幅列 → pct_chg 由前后收盘算。
    symbol: 指数裸代码如 '000688';asset_code = ('sz' if 399 else 'sh') + symbol。
    """
    with direct_connection():
        try:
            import akshare as ak
        except Exception:
            return []
        try:
            sina_sym = ("sz" if symbol.startswith("399") else "sh") + symbol
            df = ak.stock_zh_index_daily(symbol=sina_sym)
        except Exception:
            return []
    if df is None or df.empty:
        return []
    asset_code = ("sz" if symbol.startswith("399") else "sh") + symbol
    start_d = pd.to_datetime(start).date()
    end_d = pd.to_datetime(end).date()
    df = df.sort_values("date")              # 升序,供 pct_chg 前后项
    results: list[dict] = []
    prev_close: float | None = None
    for _, r in df.iterrows():
        try:
            d = pd.to_datetime(r["date"]).date()
        except (KeyError, ValueError, TypeError):
            continue
        close_val = float(r["close"]) if pd.notna(r.get("close")) else None
        if close_val is None:
            continue
        pct = round((close_val / prev_close - 1) * 100, 3) if prev_close else None
        prev_close = close_val
        if d < start_d or d > end_d:
            continue
        results.append({
            "asset_code": asset_code,
            "quote_date": d,
            "open": float(r["open"]) if pd.notna(r.get("open")) else None,
            "high": float(r["high"]) if pd.notna(r.get("high")) else None,
            "low": float(r["low"]) if pd.notna(r.get("low")) else None,
            "close": close_val,
            "pct_chg": pct,
            "volume": float(r["volume"]) if pd.notna(r.get("volume")) else None,
            "amount": float(r["amount"]) if pd.notna(r.get("amount")) else None,
        })
    return results


def get_sw_index_daily(symbol: str) -> list[dict]:
    """申万行业指数日 K 线(akshare index_hist_sw);未装/失败/空 → 返回 []。

    镜像 _get_index_daily_akshare 的稳健范式(惰性 import + try/except→[] + 逐行容错),
    但调 申万专用接口:ak.index_hist_sw(symbol, period="day"),全量返回(1999 起,无 start/end)。
    symbol: 申万行业指数裸 6 位代码(如 "801010");落库 asset_code = f"sw{symbol}"(与 sh000001 区分)。
    返回 list[dict],键对齐 IndexQuoteDaily(index_hist_sw 无"涨跌幅"列 → pct_chg 由前后收盘算)。
    """
    with direct_connection():
        try:
            import akshare as ak
        except Exception:
            return []
        try:
            df = ak.index_hist_sw(symbol=symbol, period="day")
        except Exception:
            return []
    if df is None or df.empty:
        return []
    df = df.sort_values("日期")              # 防御性按日期升序(供 pct_chg 前后项)
    asset_code = f"sw{symbol}"
    results: list[dict] = []
    prev_close = None
    for _, r in df.iterrows():
        try:
            d = pd.to_datetime(r["日期"]).date()
            close_val = float(r["收盘"])
            pct = round((close_val / prev_close - 1) * 100, 4) if prev_close and prev_close > 0 else None
            prev_close = close_val
            results.append({
                "asset_code": asset_code,
                "quote_date": d,
                "open": float(r["开盘"]) if pd.notna(r.get("开盘")) else None,
                "high": float(r["最高"]) if pd.notna(r.get("最高")) else None,
                "low": float(r["最低"]) if pd.notna(r.get("最低")) else None,
                "close": close_val,
                "pct_chg": pct,
                "volume": float(r["成交量"]) if pd.notna(r.get("成交量")) else None,
                "amount": float(r["成交额"]) if pd.notna(r.get("成交额")) else None,
            })
        except (KeyError, ValueError, TypeError):
            continue
    return results


def get_etf_daily(symbol: str, start: str, end: str) -> list[dict]:
    """ETF 日线(akshare fund_etf_hist_sina,不复权,覆盖上市以来全历史);未装/失败/空 → []。

    东财 fund_etf_hist_em(前复权)走 push2his,易被限流;改用新浪(不同主机,稳定)。
    symbol: ETF 6 位代码(如 "512800");新浪需交易所前缀(5/6/9→sh,其余→sz),内部转换。
    返回 list[dict],键对齐 IndexQuoteDaily/EtfQuoteDaily(asset_code 用裸 6 位);新浪无涨跌幅列→前后收盘算。
    start/end 客户端裁剪(新浪接口不接受日期参数)。ETF 分红小,不复权 vs 前复权差异有限(探测可接受)。
    """
    with direct_connection():
        try:
            import akshare as ak
        except Exception:
            return []
        try:
            sina_sym = ("sh" if symbol[0] in ("5", "6", "9") else "sz") + symbol
            df = ak.fund_etf_hist_sina(symbol=sina_sym)
        except Exception:
            return []
    if df is None or df.empty:
        return []
    df = df.sort_values("date")
    d0, d1 = start.replace("-", ""), end.replace("-", "")
    results: list[dict] = []
    prev_close = None
    for _, r in df.iterrows():
        try:
            d = pd.to_datetime(r["date"]).date()
            ds = d.strftime("%Y%m%d")
            if ds < d0 or ds > d1:
                prev_close = float(r["close"])
                continue
            close_val = float(r["close"])
            pct = (round((close_val / prev_close - 1) * 100, 4)
                   if prev_close and prev_close > 0 else None)
            prev_close = close_val
            results.append({
                "asset_code": symbol,
                "quote_date": d,
                "open": float(r["open"]) if pd.notna(r.get("open")) else None,
                "high": float(r["high"]) if pd.notna(r.get("high")) else None,
                "low": float(r["low"]) if pd.notna(r.get("low")) else None,
                "close": close_val,
                "pct_chg": pct,
                "volume": float(r["volume"]) if pd.notna(r.get("volume")) else None,
                "amount": float(r["amount"]) if pd.notna(r.get("amount")) else None,
            })
        except (KeyError, ValueError, TypeError):
            continue
    return results


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

    # -------- 板块（同花顺：板块K线+成交额 / 板块资金流 / 大盘资金流）--------
    # 同花顺端点绕开东财 push2/push2his 限流，是板块历史数据的主力源。
    def get_sector_kline(self, sector_name: str, days: int = 1460) -> list:
        """行业板块指数历史K线（同花顺 stock_board_industry_index_ths）。

        绕开东财 push2his 限流，能拉到当天，4年+逐日。返回 list[KlineBar]，
        失败返回 []。注意同花顺 OHLC 列名带「价」后缀（开盘价/最高价/...），区别于东财。
        """
        from datetime import date as _d, timedelta as _td
        end = _d.today().strftime("%Y%m%d")
        start = (_d.today() - _td(days=days + 30)).strftime("%Y%m%d")
        df, _, _ = _call_df([("stock_board_industry_index_ths", {
            "symbol": sector_name, "start_date": start, "end_date": end,
        })])
        if df is None or getattr(df, "empty", True):
            return []
        bars: list = []
        for _, r in df.iterrows():
            try:
                d = pd.to_datetime(pick_col(r, ["日期", "日期时间"])).date()
                bars.append(KlineBar(
                    date=d,
                    open=safe_float(pick_col(r, ["开盘价", "开盘"])) or 0.0,
                    high=safe_float(pick_col(r, ["最高价", "最高"])) or 0.0,
                    low=safe_float(pick_col(r, ["最低价", "最低"])) or 0.0,
                    close=safe_float(pick_col(r, ["收盘价", "收盘"])) or 0.0,
                    volume=safe_float(pick_col(r, ["成交量"])),
                    amount=safe_float(pick_col(r, ["成交额"])),
                ))
            except (KeyError, ValueError, TypeError):
                continue
        return bars[-days:] if days and len(bars) > days else bars

    def get_sector_flow_today(self) -> list:
        """行业板块当日资金流（同花顺 stock_fund_flow_industry 即时，90 行业）。

        比东财 rank 列更全（流入/流出/净额/公司家数/领涨股）且不受 push2 限流。
        返回 [{name, net_inflow, inflow, outflow, company_count, leading_stock,
        leading_chg, index_pct_chg}, ...]，失败返回 []。
        """
        df, _, _ = _call_df([
            ("stock_fund_flow_industry", {"symbol": "即时"}),
            ("stock_fund_flow_industry", {"indicator": "即时"}),
        ])
        if df is None or getattr(df, "empty", True):
            return []
        net_col = _find_col(df.columns, "净额", exclude=("占比",))
        out: list = []
        for _, r in df.iterrows():
            name = safe_str(pick_col(r, ["行业", "名称", "板块"]))
            if not name:
                continue
            cc = safe_float(pick_col(r, ["公司家数", "家数"]))
            out.append({
                "name": name,
                "net_inflow": safe_float(r[net_col]) if net_col else None,
                "inflow": safe_float(pick_col(r, ["流入资金", "流入"])),
                "outflow": safe_float(pick_col(r, ["流出资金", "流出"])),
                "company_count": int(cc) if cc is not None else None,
                "leading_stock": safe_str(pick_col(r, ["领涨股"])),
                "leading_chg": safe_float(pick_col(r, ["领涨股-涨跌幅"])),
                "index_pct_chg": safe_float(pick_col(r, ["行业-涨跌幅", "涨跌幅"])),
            })
        return out

    def get_market_fund_flow(self) -> list:
        """大盘资金流历史（akshare stock_market_fund_flow，~6个月逐日）。

        返回 [{date, main_net, main_pct, super_net, super_pct, large_net, large_pct,
        mid_net, mid_pct, small_net, small_pct}, ...]——主力/超大/大/中/小单净额+占比。
        失败返回 []。
        """
        df, _, _ = _call_df([("stock_market_fund_flow", {})])
        if df is None or getattr(df, "empty", True):
            return []
        date_col = next((c for c in df.columns if "日期" in str(c)), None)
        # 列名「X净流入-净额」vs「X净流入-净占比」；大单需 exclude 超大单
        flow_cols = {
            "main_net": _find_col(df.columns, "主力", "净额"),
            "main_pct": _find_col(df.columns, "主力", "占比"),
            "super_net": _find_col(df.columns, "超大单", "净额"),
            "super_pct": _find_col(df.columns, "超大单", "占比"),
            "large_net": _find_col(df.columns, "大单", "净额", exclude=("超大",)),
            "large_pct": _find_col(df.columns, "大单", "占比", exclude=("超大",)),
            "mid_net": _find_col(df.columns, "中单", "净额"),
            "mid_pct": _find_col(df.columns, "中单", "占比"),
            "small_net": _find_col(df.columns, "小单", "净额"),
            "small_pct": _find_col(df.columns, "小单", "占比"),
        }
        out: list = []
        for _, r in df.iterrows():
            if date_col is None:
                continue
            try:
                d = pd.to_datetime(r[date_col]).date()
            except (KeyError, ValueError, TypeError):
                continue
            row = {"date": d}
            for k, c in flow_cols.items():
                row[k] = safe_float(r[c]) if c else None
            out.append(row)
        return out
