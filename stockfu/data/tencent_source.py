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
