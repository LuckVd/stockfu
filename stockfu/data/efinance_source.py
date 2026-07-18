"""efinance 数据源：A 股实时行情（主力）+ 日 K 线。

efinance 免费无 token，get_realtime_quotes 支持指定代码批量，是 A 股实时行情首选源。
列名容错：不同版本列名略有差异，统一用关键词匹配。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from stockfu.data.base import (DataSource, KlineBar, Market, Quote, currency_of,
                            direct_connection)


def _f(v) -> Optional[float]:
    try:
        s = str(v).strip().replace(",", "").replace("%", "")
        return float(s) if s and s not in ("-", "--") else None
    except (TypeError, ValueError):
        return None


def _col(row, *keys) -> Optional[object]:
    for c in row.index:
        cs = str(c)
        if any(k in cs for k in keys):
            v = row.get(c)
            if v is not None and str(v).strip() not in ("", "-", "nan", "None"):
                return v
    return None


def _to_date(v) -> Optional[date]:
    try:
        return datetime.strptime(str(v).split(" ")[0], "%Y-%m-%d").date()
    except Exception:
        return None


class EfinanceSource(DataSource):
    name = "efinance"
    supports = {Market.CN}

    def _cn_symbol(self, code: str) -> str:
        return code if code.isdigit() else code.lstrip("SH").lstrip("SZ")

    def _fetch_quote(self, code: str) -> Optional[Quote]:
        """A 股现价：用稳定的 get_quote_history 取最新日K收盘价。

        注：get_realtime_quotes 接口已损坏（KeyError「行情参数不正确」），
        改用日K最新一根——名称取「股票名称」列(中文)，现价取「收盘」。
        天级近似：最新一根为最近交易日收盘（盘中则为前一交易日）。
        """
        from datetime import date as _d, timedelta as _td

        sym = self._cn_symbol(code)
        beg = (_d.today() - _td(days=30)).strftime("%Y%m%d")
        try:
            with direct_connection():  # efinance 必须 import+调用都在无代理环境
                import efinance as ef
                df = ef.stock.get_quote_history(sym, klt=101, beg=beg)
        except Exception:
            return None
        if df is None or getattr(df, "empty", True):
            return None
        r = df.iloc[-1]  # 最新一根日K = 最近交易日
        close = _f(r.get("收盘")) or 0.0
        chg = _f(r.get("涨跌额")) or 0.0
        return Quote(
            code=code,
            name=str(r.get("股票名称") or ""),
            market=Market.CN,
            currency=currency_of(Market.CN),
            price=close,
            pct_chg=_f(r.get("涨跌幅")),
            open=_f(r.get("开盘")),
            high=_f(r.get("最高")),
            low=_f(r.get("最低")),
            pre_close=close - chg,  # 昨收 ≈ 收盘 - 涨跌额
            volume=_f(r.get("成交量")),
            amount=_f(r.get("成交额")),
            pe=None, pb=None, market_cap=None,  # 日K无估值字段（akshare spot 有，但不稳）
            updated_at=datetime.now(),
        )

    def get_kline(self, code: str, days: int = 365) -> list[KlineBar]:
        from datetime import date as _d, timedelta as _td
        sym = self._cn_symbol(code)
        beg = (_d.today() - _td(days=days + 30)).strftime("%Y%m%d")
        try:
            with direct_connection():  # efinance 必须 import+调用都在无代理环境
                import efinance as ef
                df = ef.stock.get_quote_history(sym, klt=101, beg=beg)  # 101=日K, beg=起始日
        except Exception:
            return []
        if df is None or getattr(df, "empty", True):
            return []
        bars: list[KlineBar] = []
        for _, r in df.tail(days).iterrows():
            d = _to_date(r.get("日期"))
            if not d:
                continue
            bars.append(KlineBar(
                date=d,
                open=_f(r.get("开盘")) or _f(r.get("开盘价")) or 0.0,
                high=_f(r.get("最高")) or _f(r.get("最高价")) or 0.0,
                low=_f(r.get("最低")) or _f(r.get("最低价")) or 0.0,
                close=_f(r.get("收盘")) or _f(r.get("收盘价")) or 0.0,
                volume=_f(r.get("成交量")),
                amount=_f(r.get("成交额")),
            ))
        return bars
