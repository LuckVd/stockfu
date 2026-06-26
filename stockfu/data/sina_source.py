"""新浪日K数据源：A 股天级收盘的独立 backup（不走东财）。

接口 money.finance.sina.com.cn —— 独立于东财，不受东财限流影响。
定位「只要天级收盘」：仅日K(scale=240)，无盘中实时，现价=最近交易日收盘。
作为 efinance/腾讯 之后的独立第三源。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from stockfu.data.base import (DataSource, KlineBar, Market, Quote, currency_of,
                            direct_connection)

_URL = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
_UA = {"User-Agent": "Mozilla/5.0"}


def _sina_sym(code: str) -> str:
    return ("sh" if code[0] in ("6", "9", "5") else "sz") + code


def _f(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class SinaSource(DataSource):
    name = "sina"
    supports = {Market.CN}

    def _klines(self, code: str, datalen: int = 1023) -> list[KlineBar]:
        import requests

        sym = _sina_sym(code)
        try:
            with direct_connection():
                r = requests.get(_URL, params={"symbol": sym, "scale": 240,
                                               "datalen": datalen, "ma": "no"},
                                 headers=_UA, timeout=10)
        except Exception:  # noqa: BLE001
            return []
        if r.status_code != 200:
            return []
        try:
            rows = r.json()
        except Exception:  # noqa: BLE001
            return []
        bars: list[KlineBar] = []
        for row in rows:
            try:
                d = datetime.strptime(str(row["day"]).split(" ")[0], "%Y-%m-%d").date()
            except Exception:  # noqa: BLE001
                continue
            bars.append(KlineBar(
                date=d,
                open=_f(row["open"]) or 0.0, close=_f(row["close"]) or 0.0,
                high=_f(row["high"]) or 0.0, low=_f(row["low"]) or 0.0,
                volume=_f(row["volume"]),
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
            code=code, name="",  # 新浪日K接口不返回名称，由主力源 efinance 提供
            market=Market.CN, currency=currency_of(Market.CN),
            price=last.close, open=last.open, high=last.high, low=last.low,
            pre_close=prev, volume=last.volume,
            pct_chg=(((last.close - prev) / prev * 100) if prev else None),
            updated_at=datetime.now(),
        )

    def get_kline(self, code: str, days: int = 365) -> list[KlineBar]:
        bars = self._klines(code, max(days, 365))
        return bars[-days:] if days and len(bars) > days else bars
