"""通达信(pytdx)日K数据源：A 股天级收盘的最稳独立 backup。

TCP 协议直连通达信行情服务器，**不走 Web / 不走东财**，独立性与稳定性最高。
定位「只要天级收盘」：仅日K，无盘中实时，现价=最近交易日收盘。
依赖 `pip install pytdx`（import 在方法内，未装时该源自动不可用、不影响其他源）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from stockfu.data.base import DataSource, KlineBar, Market, Quote, currency_of

# 通达信行情服务器池（pytdx 自带更多，这里列实测可用的几个）
_TDX_HOSTS = ["115.238.56.198", "115.238.90.165", "119.147.212.81", "112.74.214.43"]


def _tdx_market_code(code: str) -> tuple[int, str]:
    """通达信市场：1=沪，0=深。6/9/5 开头→沪，其余→深。"""
    return (1, code) if code[0] in ("6", "9", "5") else (0, code)


class PytdxSource(DataSource):
    name = "pytdx"
    supports = {Market.CN}

    def _klines(self, code: str, count: int = 800) -> list[KlineBar]:
        try:
            from pytdx.hq import TdxHq_API
        except Exception:  # noqa: BLE001  未装 pytdx → 该源不可用
            return []
        market, sym = _tdx_market_code(code)
        api = TdxHq_API(heartbeat=False)
        bars: list[KlineBar] = []
        for ip in _TDX_HOSTS:
            try:
                with api.connect(ip, 7709, time_out=5):
                    raw = api.get_security_bars(4, market, sym, 0, count)  # 4=日K
                    for b in (raw or []):
                        try:
                            d = datetime.strptime(
                                str(b["datetime"]).split(" ")[0], "%Y-%m-%d").date()
                        except Exception:  # noqa: BLE001
                            continue
                        bars.append(KlineBar(
                            date=d,
                            open=float(b["open"]), close=float(b["close"]),
                            high=float(b["high"]), low=float(b["low"]),
                            volume=float(b.get("vol") or 0),
                            amount=float(b.get("amount") or 0),  # 通达信有成交额
                        ))
                    if bars:
                        break
            except Exception:  # noqa: BLE001  该服务器失败，换下一个
                continue
        return bars

    def _fetch_quote(self, code: str) -> Optional[Quote]:
        """现价 = 最近交易日收盘价（天级，无盘中实时）。"""
        bars = self._klines(code, 5)
        if not bars:
            return None
        last = bars[-1]
        prev = bars[-2].close if len(bars) >= 2 else last.close
        return Quote(
            code=code, name="",  # 通达信不返回股票名称
            market=Market.CN, currency=currency_of(Market.CN),
            price=last.close, open=last.open, high=last.high, low=last.low,
            pre_close=prev, volume=last.volume,
            pct_chg=(((last.close - prev) / prev * 100) if prev else None),
            updated_at=datetime.now(),
        )

    def get_kline(self, code: str, days: int = 365) -> list[KlineBar]:
        bars = self._klines(code, max(days, 800))
        return bars[-days:] if days and len(bars) > days else bars
