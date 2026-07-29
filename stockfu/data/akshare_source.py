"""akshare 数据源：A 股分红（主力）+ 个股/板块资金流 + ETF 份额 + 实时兜底。

分红：优先 stock_history_dividend_detail（「派息」列为每 10 股口径，每股 = 派息/10，
最稳定可靠）；fhps/cninfo 作文本解析兜底。列名容错借鉴
daily_stock_analysis/data_provider/fundamental_adapter.py (MIT)。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, date, timedelta
from typing import Optional

import pandas as pd

from stockfu.data.base import (DataSource, KlineBar, Market, Quote, currency_of,
                            detect_market, direct_connection)
from stockfu.data.dividend_parser import (_filter_rows as filter_rows,
                                       _pick as pick_col, build_metric_from_df,
                                       build_metric_from_fhps, build_metric_from_history,
                                       safe_float, safe_str)


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
    # 399xxx = 深证(创业板指 399006 等)→ sz；其余(上证 000001/科创 000688 等)→ sh
    asset_code = ("sz" if symbol.startswith("399") else "sh") + symbol
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
    """ETF 日线(**前复权 qfq**);未装/失败/空 → []。

    硬约束：禁止不复权(已移除 fund_etf_hist_sina 路径)；**禁止 baostock**
    （ETF 历史仅约半年且 adjustflag 实际等于不复权）。
    主源：东财 `fund_etf_hist_em(adjust="qfq")`（重试）；失败/空 → 腾讯 qfq 兜底。
    symbol: ETF 6 位代码(如 "512800")。
    返回 list[dict],键对齐 EtfQuoteDaily(asset_code 用裸 6 位)。
    start/end: "YYYY-MM-DD"。
    """
    rows = _get_etf_daily_em_qfq(symbol, start, end)
    if rows:
        return rows
    return _get_etf_daily_tencent_qfq(symbol, start, end)


def _get_etf_daily_em_qfq(symbol: str, start: str, end: str,
                          retries: int = 3) -> list[dict]:
    """东财 fund_etf_hist_em 前复权(带重试；东财易断连)。"""
    d0 = start.replace("-", "")
    d1 = end.replace("-", "")
    for attempt in range(max(1, retries)):
        with direct_connection():
            try:
                import akshare as ak
            except Exception:
                return []
            try:
                df = ak.fund_etf_hist_em(
                    symbol=symbol, period="daily", adjust="qfq",
                    start_date=d0, end_date=d1,
                )
            except Exception:  # noqa: BLE001
                df = None
        if df is not None and not getattr(df, "empty", True):
            return _etf_df_to_rows(symbol, df)
        if attempt + 1 < retries:
            time.sleep(0.8 * (attempt + 1))
    return []


def _etf_df_to_rows(symbol: str, df: pd.DataFrame) -> list[dict]:
    results: list[dict] = []
    for _, r in df.iterrows():
        try:
            d = pd.to_datetime(r["日期"]).date()
            close_val = float(r["收盘"])
            results.append({
                "asset_code": symbol,
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


def _get_etf_daily_tencent_qfq(symbol: str, start: str, end: str) -> list[dict]:
    """腾讯 fqkline 前复权兜底(**按日期分段拉取,可覆盖 2021+ 全周期**)。

    注意：
    - 无 start/end 时单次约 640–800 根;指定 `start,end` 可按窗翻页。
    - 实测分段 qfq 重叠日 **100% 一致**(同一前复权基准),可安全 merge。
    - 部分 ETF 只返回 `day` 无 `qfqday` → 用 day(同参 qfq 请求降级)。
    - 不走 TencentSource(其硬读 qfqday 且无日期翻页)。
    """
    import requests
    from datetime import datetime as _dt, timedelta as _td

    start_d = pd.to_datetime(start).date()
    end_d = pd.to_datetime(end).date()
    if start_d > end_d:
        return []

    sym = ("sh" if symbol[0] in ("6", "9", "5") else "sz") + symbol
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}

    # 约 2 年一段(≈500 交易日),低于 800 上限;重叠由 merge 去重
    chunk_days = 730
    by_date: dict = {}
    cur = start_d
    while cur <= end_d:
        chunk_end = min(cur + _td(days=chunk_days), end_d)
        param = f"{sym},day,{cur.isoformat()},{chunk_end.isoformat()},800,qfq"
        try:
            with direct_connection():
                r = requests.get(url, params={"param": param},
                                 headers=headers, timeout=20)
            data = (r.json().get("data") or {}).get(sym) or {}
            rows = data.get("qfqday") or data.get("day") or []
        except Exception:
            rows = []
        for row in rows:
            try:
                d = _dt.strptime(str(row[0]).split(" ")[0], "%Y-%m-%d").date()
                o = float(row[1]); c = float(row[2])
                h = float(row[3]); lo = float(row[4])
                vol = (float(row[5]) if len(row) > 5 and row[5] not in (None, "")
                       else None)
            except (TypeError, ValueError, IndexError):
                continue
            if d < start_d or d > end_d:
                continue
            by_date[d] = (o, h, lo, c, vol)
        # 下一段:从本段末日往后(有数据则从最后一天+1,否则跳 chunk)
        if rows:
            last_d = max(by_date) if by_date else chunk_end
            nxt = last_d + _td(days=1)
            # 防死循环:至少前进
            cur = max(nxt, cur + _td(days=1))
        else:
            cur = chunk_end + _td(days=1)
        time.sleep(0.15)

    if not by_date:
        return []
    results: list[dict] = []
    prev_close = None
    for d in sorted(by_date):
        o, h, lo, c, vol = by_date[d]
        pct = (round((c / prev_close - 1) * 100, 4)
               if prev_close and prev_close > 0 else None)
        if c:
            prev_close = c
        results.append({
            "asset_code": symbol,
            "quote_date": d,
            "open": o, "high": h, "low": lo, "close": c,
            "pct_chg": pct, "volume": vol, "amount": None,
        })
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
    _ths_catalog: list[dict] | None = None
    _ths_catalog_ts: float = 0.0

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
    def get_sector_catalog_ths(self) -> list[dict]:
        """同花顺标准行业清单（名称+代码），缓存 10 分钟。"""
        if self._ths_catalog is not None and time.monotonic() - self._ths_catalog_ts < 600:
            return self._ths_catalog
        df, _, _ = _call_df([("stock_board_industry_name_ths", {})])
        if df is None or getattr(df, "empty", True):
            return []
        out, seen = [], set()
        for _, r in df.iterrows():
            name = safe_str(pick_col(r, ["name", "名称", "行业", "板块名称"]))
            code = safe_str(pick_col(r, ["code", "代码", "板块代码"]))
            if name and code and name not in seen:
                out.append({"name": name, "code": code})
                seen.add(name)
        self._ths_catalog, self._ths_catalog_ts = out, time.monotonic()
        return out

    def get_sector_names_ths(self) -> list[str]:
        """同花顺标准行业名称清单（当前预期 90 个）。"""
        return [x["name"] for x in self.get_sector_catalog_ths()]

    def get_sector_kline_period(self, sector_name: str, start_date: str, end_date: str) -> list:
        """同花顺单行业、单年度日线端点，并合并交易日 ``today.js``。

        失败可见 + 轻量重试:requests 异常或非 200 自动重试 2 次(退避 0.6/1.2s,均 > 0.3s
        安全间隔);最终失败记 warning(带状态码/异常)后返回 []——保持调用方接口不变,
        但端点故障不再被静默吞掉(真无数据 = HTTP 200 但 data 空,不重试)。年度文件
        在收盘当天仍可能只到 T-1，故请求区间包含今天时额外读取同花顺实时 ``today.js``。
        """
        import logging
        log = logging.getLogger("stockfu")
        try:
            start, end = datetime.strptime(start_date, "%Y%m%d").date(), datetime.strptime(end_date, "%Y%m%d").date()
        except ValueError:
            return []
        catalog = {x["name"]: x["code"] for x in self.get_sector_catalog_ths()}
        code = catalog.get(sector_name)
        if not code:
            log.warning("sector_kline_period: %s 不在同花顺分类清单(共%d个行业)", sector_name, len(catalog))
            return []
        import requests
        url = f"https://d.10jqka.com.cn/v4/line/bk_{code}/01/{start.year}.js"
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "http://q.10jqka.com.cn/"}
        raw = None
        last_err = None
        for attempt in range(3):              # 初试 + 2 次重试
            try:
                with direct_connection():
                    response = requests.get(url, headers=headers, timeout=20)
                if response.status_code != 200:
                    last_err = f"HTTP {response.status_code}"
                else:
                    raw = response.text
                    break
            except Exception as exc:          # noqa: BLE001  连接错/超时
                last_err = f"{type(exc).__name__}: {exc}"
            if attempt < 2:
                time.sleep(0.6 * (attempt + 1))   # 0.6s → 1.2s(> 同花顺 0.3s 安全间隔)
        if raw is None:
            log.warning("sector_kline_period: %s(%s) %d年 拉取失败(%s)—重试耗尽,疑似端点波动/反爬升级",
                        sector_name, code, start.year, last_err)
            return []
        try:
            payload = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
            lines = str(payload.get("data") or "").split(";")
        except Exception as exc:              # noqa: BLE001  200 但正文非 JSON(反爬页)
            log.warning("sector_kline_period: %s(%s) 正文解析失败(%s) head=%r",
                        sector_name, code, exc, raw[:120])
            return []
        bars = []
        for line in lines:
            try:
                fields = line.split(",")
                d = datetime.strptime(fields[0], "%Y%m%d").date()
                if d < start or d > end:
                    continue
                bars.append(KlineBar(
                    date=d,
                    open=safe_float(fields[1]) or 0.0, high=safe_float(fields[2]) or 0.0,
                    low=safe_float(fields[3]) or 0.0, close=safe_float(fields[4]) or 0.0,
                    volume=safe_float(fields[5]), amount=safe_float(fields[6]),
                ))
            except (IndexError, ValueError, TypeError):
                continue
        # 年度归档文件通常 T+1 才写入当天；today.js 则是交易日的实时/收盘日线。
        # 只在调用方明确请求本机当天时合并，避免把当前快照误当作历史日期的数据。
        current = date.today()
        if start <= current <= end:
            today_url = f"https://d.10jqka.com.cn/v4/line/bk_{code}/01/today.js"
            try:
                with direct_connection():
                    response = requests.get(today_url, headers=headers, timeout=20)
                if response.status_code != 200:
                    raise RuntimeError(f"HTTP {response.status_code}")
                payload = json.loads(response.text[response.text.find("{"):response.text.rfind("}") + 1])
                row = payload.get(f"bk_{code}") or next(iter(payload.values()), {})
                bar_date = datetime.strptime(str(row.get("1") or ""), "%Y%m%d").date()
                if start <= bar_date <= end:
                    today_bar = KlineBar(
                        date=bar_date,
                        open=safe_float(row.get("7")) or 0.0,
                        high=safe_float(row.get("8")) or 0.0,
                        low=safe_float(row.get("9")) or 0.0,
                        close=safe_float(row.get("11")) or 0.0,
                        volume=safe_float(row.get("13")),
                        amount=safe_float(row.get("19")),
                    )
                    bars = [bar for bar in bars if bar.date != bar_date]
                    bars.append(today_bar)
            except Exception as exc:  # noqa: BLE001  today.js 非交易日或短暂不可用均可回退历史
                log.warning("sector_kline_period: %s(%s) today.js 未合并(%s)",
                            sector_name, code, exc)
        bars.sort(key=lambda bar: bar.date)
        return bars

    def get_sector_kline(self, sector_name: str, days: int = 1460) -> list:
        """行业板块指数历史K线（同花顺 stock_board_industry_index_ths）。

        绕开东财 push2his 限流，能拉到当天，4年+逐日。返回 list[KlineBar]，
        失败返回 []。注意同花顺 OHLC 列名带「价」后缀（开盘价/最高价/...），区别于东财。
        """
        from datetime import date as _d, timedelta as _td
        end_d = _d.today()
        start_d = _d.today() - _td(days=days + 30)
        bars = []
        for year in range(start_d.year, end_d.year + 1):
            lo = max(start_d, _d(year, 1, 1)).strftime("%Y%m%d")
            hi = min(end_d, _d(year, 12, 31)).strftime("%Y%m%d")
            bars.extend(self.get_sector_kline_period(sector_name, lo, hi))
        bars.sort(key=lambda x: x.date)
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

    def get_sector_flow_history(self, sector_name: str) -> list[dict]:
        """东财行业历史主力资金流（近期窗口）。失败返回空。

        这个接口按行业逐个请求，调用方必须串行并限频；这里不做重试或并发，
        以免初始化全行业历史时触发数据源限流。
        """
        with direct_connection():
            try:
                import akshare as ak
                df = ak.stock_sector_fund_flow_hist(symbol=sector_name)
            except Exception:
                return []
        if df is None or df.empty:
            return []
        out: list[dict] = []
        for _, r in df.iterrows():
            try:
                d = pd.to_datetime(pick_col(r, ["日期"])).date()
            except (KeyError, ValueError, TypeError):
                continue
            out.append({
                "date": d,
                "net_inflow": safe_float(pick_col(r, ["主力净流入-净额"])),
                "net_inflow_pct": safe_float(pick_col(r, ["主力净流入-净占比"])),
            })
        return out

    def get_sector_names_em(self) -> list[str]:
        """东方财富行业分类清单；与其历史资金流接口保持同一分类。"""
        with direct_connection():
            try:
                import akshare as ak
                df = ak.stock_board_industry_name_em()
            except Exception:
                return []
        if df is None or df.empty:
            return []
        return list(dict.fromkeys(
            safe_str(pick_col(r, ["板块名称", "名称", "行业名称"])) for _, r in df.iterrows()
            if safe_str(pick_col(r, ["板块名称", "名称", "行业名称"]))
        ))

    def get_sector_kline_em(self, sector_name: str) -> list:
        """东方财富行业历史日线，与 get_sector_flow_history 同分类。"""
        with direct_connection():
            try:
                import akshare as ak
                df = ak.stock_board_industry_hist_em(
                    symbol=sector_name, start_date="20200101",
                    end_date=date.today().strftime("%Y%m%d"), period="日k", adjust="")
            except Exception:
                return []
        if df is None or df.empty:
            return []
        out = []
        for _, r in df.iterrows():
            try:
                d = pd.to_datetime(pick_col(r, ["日期"])).date()
                out.append(KlineBar(date=d,
                    open=safe_float(pick_col(r, ["开盘"])) or 0.0,
                    high=safe_float(pick_col(r, ["最高"])) or 0.0,
                    low=safe_float(pick_col(r, ["最低"])) or 0.0,
                    close=safe_float(pick_col(r, ["收盘"])) or 0.0,
                    volume=safe_float(pick_col(r, ["成交量"])),
                    amount=safe_float(pick_col(r, ["成交额"]))))
            except (KeyError, ValueError, TypeError):
                continue
        return out

    def get_sector_spot_em(self) -> list[dict]:
        """东方财富行业当日行情批量表，一次请求覆盖全部行业。"""
        with direct_connection():
            try:
                import akshare as ak
                df = ak.stock_board_industry_spot_em()
            except Exception:
                return []
        if df is None or df.empty:
            return []
        out = []
        for _, r in df.iterrows():
            name = safe_str(pick_col(r, ["板块名称", "名称"]))
            if not name:
                continue
            out.append({"name": name,
                "close": safe_float(pick_col(r, ["最新价", "最新", "收盘"])),
                "pct_chg": safe_float(pick_col(r, ["涨跌幅"])),
                "amount": safe_float(pick_col(r, ["成交额"])),
                "volume": safe_float(pick_col(r, ["成交量"]))})
        return out

    def get_sector_flow_today_em(self) -> list[dict]:
        """东方财富行业当日主力资金流批量表，一次请求覆盖全部行业。"""
        df, _, _ = _call_df([("stock_sector_fund_flow_rank", {
            "indicator": "今日", "sector_type": "行业资金流"})])
        if df is None or df.empty:
            return []
        out = []
        for _, r in df.iterrows():
            name = safe_str(pick_col(r, ["名称", "行业", "板块名称"]))
            if name:
                out.append({"name": name,
                    "net_inflow": safe_float(pick_col(r, ["主力净流入-净额", "主力净流入"])),
                    "net_inflow_pct": safe_float(pick_col(r, ["主力净流入-净占比"])),
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
