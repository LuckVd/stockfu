"""腾讯日K数据源(前复权 qfq)：A 股天级收盘数据的独立 fallback。

接口 web.ifzq.gtimg.cn/appstock/app/fqkline/get —— **不走东财**，不受东财限流/反爬
影响。定位「只要天级收盘」：仅日K，无盘中实时，现价=最近交易日收盘。
作为 efinance(东财) 之后的独立第二源：东财挂了，A 股收盘数据仍能从腾讯取到。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from stockfu.data.base import (DataSource, KlineBar, Market, Quote, currency_of,
                            direct_connection)

_UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


def _tencent_sym(code: str) -> str:
    """A 股 6 位代码 → 腾讯符号：6/9/5 开头→sh，其余→sz。"""
    return ("sh" if code[0] in ("6", "9", "5") else "sz") + code


def _f(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class TencentSource(DataSource):
    name = "tencent"
    supports = {Market.CN}

    def _klines(self, code: str, count: int = 640) -> list[KlineBar]:
        import requests

        sym = _tencent_sym(code)
        try:
            with direct_connection():  # 国内源直连
                r = requests.get(_URL, params={"param": f"{sym},day,,,{count},qfq"},
                                 headers=_UA, timeout=10)
        except Exception:  # noqa: BLE001
            return []
        if r.status_code != 200:
            return []
        try:
            rows = r.json()["data"][sym]["qfqday"]
        except Exception:  # noqa: BLE001
            return []
        bars: list[KlineBar] = []
        for row in rows:
            # 腾讯格式：[日期, 开, 收, 高, 低, 成交量]
            try:
                d = datetime.strptime(str(row[0]).split(" ")[0], "%Y-%m-%d").date()
            except Exception:  # noqa: BLE001
                continue
            bars.append(KlineBar(
                date=d,
                open=_f(row[1]) or 0.0, close=_f(row[2]) or 0.0,
                high=_f(row[3]) or 0.0, low=_f(row[4]) or 0.0,
                volume=_f(row[5]),  # 腾讯日K无成交额字段
            ))
        return bars

    def _fetch_quote(self, code: str) -> Optional[Quote]:
        """现价 = 最近交易日收盘价（天级，无盘中实时）。"""
        bars = self._klines(code, 5)
        if not bars:
            return None
        last = bars[-1]
        prev = bars[-2].close if len(bars) >= 2 else last.close
        return Quote(
            code=code, name="",  # 腾讯日K接口不返回股票名称，由主力源 efinance 提供
            market=Market.CN, currency=currency_of(Market.CN),
            price=last.close, open=last.open, high=last.high, low=last.low,
            pre_close=prev, volume=last.volume,
            pct_chg=(((last.close - prev) / prev * 100) if prev else None),
            updated_at=datetime.now(),
        )

    def get_kline(self, code: str, days: int = 365) -> list[KlineBar]:
        bars = self._klines(code, max(days, 640))
        return bars[-days:] if days and len(bars) > days else bars

    def get_kline_range_adj(self, code: str, start: str, end: str,
                            adj: str = "qfq") -> list[KlineBar]:
        """按日期窗分段拉某复权口径日K。adj=qfq|raw|hfq。

        腾讯 fqkline: 参数末位空=不复权 day, qfq=qfqday, hfq=hfqday。
        约 2 年一段,低于单次 800 根上限。
        """
        import requests
        from datetime import datetime as _dt, timedelta as _td

        start_d = _dt.strptime(start[:10], "%Y-%m-%d").date()
        end_d = _dt.strptime(end[:10], "%Y-%m-%d").date()
        if start_d > end_d:
            return []
        adj_n = (adj or "qfq").lower()
        # 腾讯: 空后缀→不复权 day; qfq→qfqday; hfq→hfqday
        if adj_n == "raw":
            suffix, key = "", "day"
        elif adj_n == "hfq":
            suffix, key = "hfq", "hfqday"
        else:
            suffix, key = "qfq", "qfqday"

        sym = _tencent_sym(code)
        by_date: dict = {}
        cur = start_d
        chunk_days = 730
        while cur <= end_d:
            chunk_end = min(cur + _td(days=chunk_days), end_d)
            param = f"{sym},day,{cur.isoformat()},{chunk_end.isoformat()},800,{suffix}"
            try:
                with direct_connection():
                    r = requests.get(_URL, params={"param": param},
                                     headers=_UA, timeout=20)
                data = (r.json().get("data") or {}).get(sym) or {}
                rows = data.get(key) or data.get("day") or []
            except Exception:  # noqa: BLE001
                rows = []
            for row in rows:
                try:
                    d = _dt.strptime(str(row[0]).split(" ")[0], "%Y-%m-%d").date()
                    o = _f(row[1]) or 0.0
                    c = _f(row[2]) or 0.0
                    h = _f(row[3]) or 0.0
                    lo = _f(row[4]) or 0.0
                    vol = _f(row[5]) if len(row) > 5 else None
                except Exception:  # noqa: BLE001
                    continue
                if d < start_d or d > end_d:
                    continue
                by_date[d] = KlineBar(
                    date=d, open=o, high=h, low=lo, close=c, volume=vol,
                )
            cur = chunk_end + _td(days=1)
        return [by_date[d] for d in sorted(by_date)]

    def get_kline_triple(self, code: str, start: str,
                         end: str | None = None) -> dict[str, list[KlineBar]]:
        """一次拉齐三套复权 → {qfq|raw|hfq: bars}。主用于全市场三复权回补。"""
        end = end or datetime.now().strftime("%Y-%m-%d")
        out = {}
        for adj in ("qfq", "raw", "hfq"):
            try:
                out[adj] = self.get_kline_range_adj(code, start, end, adj=adj)
            except Exception:  # noqa: BLE001
                out[adj] = []
        return out
