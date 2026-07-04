"""baostock 数据源：A 股日K backup + PE/PB 历史（免费替代 tushare）。

baostock 免费无 token，提供 A 股日K（含 peTTM/pbMRQ/换手率）—— 既作 K线 backup，
又是项目长期缺失的 **PE/PB 历史分位** 的免费数据来源（原以为要 tushare ~200 元）。
依赖 `pip install baostock`（import 在方法内，未装时该源自动不可用）。

注：baostock 为进程级全局登录（_ensure_login 幂等）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from stockfu.data.base import DataSource, KlineBar, Market, Quote, currency_of


def _bs_code(code: str) -> str:
    return ("sh." if code[0] in ("6", "9", "5") else "sz.") + code


def _f(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


class BaostockSource(DataSource):
    name = "baostock"
    supports = {Market.CN}
    _logged_in: bool = False

    @classmethod
    def _ensure_login(cls, force: bool = False) -> bool:
        try:
            import baostock as bs
        except Exception:  # noqa: BLE001
            return False
        if force or not cls._logged_in:
            if force:  # 重连前先 logout，清掉可能已掉线的旧连接
                try:
                    bs.logout()
                except Exception:  # noqa: BLE001
                    pass
            lg = bs.login()
            cls._logged_in = (getattr(lg, "error_code", "1") == "0")
        return cls._logged_in

    @classmethod
    def force_relogin(cls) -> bool:
        """强制重新登录。baostock 是进程级全局连接，偶发掉线时 _logged_in 仍 True、
        _ensure_login 不会重连 → 后续 query 静默失败返回空。调用方拿到空结果后
        调此方法 logout+重新 login 即可恢复。"""
        return cls._ensure_login(force=True)

    def _klines(self, code: str, days: int = 800) -> list[KlineBar]:
        if not self._ensure_login():
            return []
        import baostock as bs
        from datetime import date as _d, timedelta as _td

        start = (_d.today() - _td(days=days + 15)).strftime("%Y-%m-%d")
        try:
            rs = bs.query_history_k_data_plus(
                _bs_code(code),
                "date,open,high,low,close,volume,amount",
                start_date=start, frequency="d", adjustflag="2")  # 2=前复权
        except Exception:  # noqa: BLE001
            return []
        bars: list[KlineBar] = []
        while getattr(rs, "error_code", "1") == "0" and rs.next():
            row = rs.get_row_data()
            try:
                d = datetime.strptime(row[0], "%Y-%m-%d").date()
            except Exception:  # noqa: BLE001
                continue
            bars.append(KlineBar(
                date=d,
                open=_f(row[1]) or 0.0, high=_f(row[2]) or 0.0,
                low=_f(row[3]) or 0.0, close=_f(row[4]) or 0.0,
                volume=_f(row[5]), amount=_f(row[6]),
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
            code=code, name="",
            market=Market.CN, currency=currency_of(Market.CN),
            price=last.close, open=last.open, high=last.high, low=last.low,
            pre_close=prev, volume=last.volume,
            pct_chg=(((last.close - prev) / prev * 100) if prev else None),
            updated_at=datetime.now(),
        )

    def get_kline(self, code: str, days: int = 365) -> list[KlineBar]:
        bars = self._klines(code, max(days, 800))
        return bars[-days:] if days and len(bars) > days else bars

    def get_pe_pb_percentile(self, code: str, years: int = 10) -> tuple[Optional[float], Optional[float]]:
        """拉 PE/PB 历史，返回当前 PE/PB 的历史分位(0-100)。免费替代 tushare。"""
        if not self._ensure_login():
            return None, None
        import baostock as bs
        from datetime import date as _d, timedelta as _td
        from stockfu.services import factors as F

        start = (_d.today() - _td(days=years * 365 + 15)).strftime("%Y-%m-%d")
        try:
            rs = bs.query_history_k_data_plus(
                _bs_code(code), "date,peTTM,pbMRQ",
                start_date=start, frequency="d")
        except Exception:  # noqa: BLE001
            return None, None
        pes: list[float] = []
        pbs: list[float] = []
        cur_pe = cur_pb = None
        while getattr(rs, "error_code", "1") == "0" and rs.next():
            row = rs.get_row_data()
            pe, pb = _f(row[1]), _f(row[2])
            if pe and pe > 0:
                pes.append(pe)
            if pb and pb > 0:
                pbs.append(pb)
            cur_pe, cur_pb = pe, pb  # 最后一行 = 最新
        pe_pct = F.percentile(pes, cur_pe)[0] if pes and cur_pe else None
        pb_pct = F.percentile(pbs, cur_pb)[0] if pbs and cur_pb else None
        return pe_pct, pb_pct
