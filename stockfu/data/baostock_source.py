"""baostock 数据源：A 股日K backup + PE/PB 历史（免费替代 tushare）。

baostock 免费无 token，提供 A 股日K（含 peTTM/pbMRQ/换手率）—— 既作 K线 backup，
又是项目长期缺失的 **PE/PB 历史分位** 的免费数据来源（原以为要 tushare ~200 元）。
依赖 `pip install baostock`（import 在方法内，未装时该源自动不可用）。

注：baostock 为进程级全局登录（_ensure_login 幂等）。
"""
from __future__ import annotations

from datetime import datetime, date
from typing import Optional

from stockfu.data.base import (
    DataSource, DividendEventDTO, DividendMetric, KlineBar, Market, Quote, currency_of,
)


def _bs_code(code: str) -> str:
    return ("sh." if code[0] in ("6", "9", "5") else "sz.") + code


def _index_bs_symbol(symbol: str) -> str:
    """指数代码 → baostock 格式：399xxx → sz.（深证系列），其余(000xxx 上证系列) → sh.。

    与股票 _bs_code 不同：000001/000300/000016 等指数属上证，不能按首位 0 判深证。
    """
    return ("sz." if symbol.startswith("399") else "sh.") + symbol


def _f(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _i(v) -> Optional[int]:
    """baostock tradestatus/isST → int;空串/异常 → None(不假装 0)。"""
    if v in (None, ""):
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _parse_date(v) -> Optional[date]:
    """baostock 'YYYY-MM-DD' 字符串 → date;空串/异常 → None。"""
    if v in (None, ""):
        return None
    try:
        return datetime.strptime(str(v).strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


class BaostockSource(DataSource):
    name = "baostock"
    supports = {Market.CN}
    _logged_in: bool = False

    @classmethod
    def _ensure_login(cls, force: bool = False) -> bool:
        """登录 baostock。

        默认经进程级免费代理池（``ensure_baostock_login``）：
        拉公网代理 → 隧道到 :10030 → 失败剔除换 IP。
        直连可用 ``BAOSTOCK_PROXY_MODE=direct`` 关闭。

        代理会话内部 raw login 时不走池，避免递归。
        """
        try:
            import baostock as bs  # noqa: F401
        except Exception:  # noqa: BLE001
            return False
        try:
            from stockfu.data.baostock_proxy import ensure_baostock_login, in_raw_login
        except Exception:  # noqa: BLE001
            return cls._login_raw(force=force)

        if in_raw_login():
            return cls._login_raw(force=force)
        return ensure_baostock_login(force=force)

    @classmethod
    def _login_raw(cls, force: bool = False) -> bool:
        """裸 login（当前 socket 已由代理池注入，或 direct 模式）。"""
        try:
            import baostock as bs
        except Exception:  # noqa: BLE001
            return False
        if force or not cls._logged_in:
            if force:
                try:
                    bs.logout()
                except Exception:  # noqa: BLE001
                    pass
            lg = bs.login()
            cls._logged_in = (getattr(lg, "error_code", "1") == "0")
        return cls._logged_in

    @classmethod
    def force_relogin(cls) -> bool:
        """强制重新登录。失败时由代理池剔除当前 IP 并切换。

        baostock 是进程级全局连接，偶发掉线时 _logged_in 仍 True、
        不 force 则后续 query 静默失败。"""
        return cls._ensure_login(force=True)

    @classmethod
    def rotate_proxy(cls, reason: str = "query_fail") -> bool:
        """查询失败后换代理并重登。"""
        try:
            from stockfu.data.baostock_proxy import rotate_baostock_proxy
            return rotate_baostock_proxy(reason)
        except Exception:  # noqa: BLE001
            return cls.force_relogin()

    # baostock adjustflag: 1=后复权 hfq, 2=前复权 qfq, 3=不复权 raw
    ADJ_FLAG = {"hfq": "1", "qfq": "2", "raw": "3"}

    def _klines(self, code: str, days: int = 800,
                adj: str = "qfq") -> list[KlineBar]:
        """日K。adj=qfq|raw|hfq → baostock adjustflag 2|3|1。默认前复权。"""
        if not self._ensure_login():
            return []
        import baostock as bs
        from datetime import date as _d, timedelta as _td

        start = (_d.today() - _td(days=days + 15)).strftime("%Y-%m-%d")
        flag = self.ADJ_FLAG.get((adj or "qfq").lower(), "2")
        try:
            # 全字段:状态 + 估值 + 换手,供补全「最新交易日所有数据」
            rs = bs.query_history_k_data_plus(
                _bs_code(code),
                "date,open,high,low,close,volume,amount,tradestatus,isST,"
                "pctChg,peTTM,pbMRQ,turn",
                start_date=start, frequency="d", adjustflag=flag)
        except Exception:  # noqa: BLE001
            return []
        return self._parse_kline_rs(rs)

    def _klines_range(self, code: str, start: str, end: str | None = None,
                      adj: str = "qfq") -> list[KlineBar]:
        """按日期区间拉日K(供三复权批量回补)。start/end=YYYY-MM-DD。

        网络类 error_code 抛 RuntimeError（供代理池剔除切换）；无数据返回 []。
        """
        if not self._ensure_login():
            raise RuntimeError("baostock not logged in")
        import baostock as bs
        flag = self.ADJ_FLAG.get((adj or "qfq").lower(), "2")
        kwargs = dict(
            start_date=start, frequency="d", adjustflag=flag,
        )
        if end:
            kwargs["end_date"] = end
        try:
            rs = bs.query_history_k_data_plus(
                _bs_code(code),
                "date,open,high,low,close,volume,amount,tradestatus,isST,"
                "pctChg,peTTM,pbMRQ,turn",
                **kwargs)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"baostock query exc: {type(e).__name__}: {e}") from e
        err = str(getattr(rs, "error_code", "1") or "1")
        if err != "0":
            msg = getattr(rs, "error_msg", "") or ""
            low = f"{err} {msg}".lower()
            # 网络/接收/连接类 → 抛错触发换代理（即便 error_code 不在 10002 段，
            # 只要 msg 含"接收数据异常/连接/超时"也认定为代理坏）
            if (
                err.startswith("10002")
                or err in ("10001001", "10001011", "10001005")
                or any(m in low for m in (
                    "接收数据异常", "连接失败", "连接超时", "timeout",
                    "receive", "you don't login",
                ))
            ):
                raise RuntimeError(f"baostock query err {err}: {msg}")
            return []
        return self._parse_kline_rs(rs)

    @staticmethod
    def _parse_kline_rs(rs) -> list[KlineBar]:
        bars: list[KlineBar] = []
        while getattr(rs, "error_code", "1") == "0" and rs.next():
            row = rs.get_row_data()
            try:
                d = datetime.strptime(row[0], "%Y-%m-%d").date()
            except Exception:  # noqa: BLE001
                continue
            # 0date 1o 2h 3l 4c 5vol 6amt 7ts 8st 9pct 10pe 11pb 12turn
            bars.append(KlineBar(
                date=d,
                open=_f(row[1]) or 0.0, high=_f(row[2]) or 0.0,
                low=_f(row[3]) or 0.0, close=_f(row[4]) or 0.0,
                volume=_f(row[5]), amount=_f(row[6]),
                trade_status=_i(row[7]) if len(row) > 7 else None,
                is_st=_i(row[8]) if len(row) > 8 else None,
                pct_chg=_f(row[9]) if len(row) > 9 else None,
                pe=_f(row[10]) if len(row) > 10 else None,
                pb=_f(row[11]) if len(row) > 11 else None,
                turnover=_f(row[12]) if len(row) > 12 else None,
            ))
        # 循环若因 error_code 变非 0 而终止（中途"接收数据异常"/掉线）→ 抛错换代理；
        # 否则残缺数据当成功写入会漏日期、污染回补。
        if getattr(rs, "error_code", "1") != "0":
            msg = getattr(rs, "error_msg", "") or ""
            raise RuntimeError(f"baostock stream broken code={rs.error_code} {msg}")
        return bars

    def get_kline_triple(self, code: str, start: str,
                         end: str | None = None) -> dict[str, list[KlineBar]]:
        """一次拉齐三套复权 K 线 → {qfq|raw|hfq: bars}。

        网络/登录错误向上抛（代理池切换）；单口径参数失败记为空列表。
        """
        out: dict[str, list[KlineBar]] = {}
        net_err: Exception | None = None
        for adj in ("qfq", "raw", "hfq"):
            try:
                out[adj] = self._klines_range(code, start, end, adj=adj)
            except RuntimeError as e:
                # 网络类：整次 triple 失败，交给代理池
                if "baostock" in str(e).lower() or "not logged" in str(e).lower():
                    net_err = e
                    break
                out[adj] = []
            except Exception:  # noqa: BLE001
                out[adj] = []
        if net_err is not None:
            raise net_err
        return out

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

    def get_kline(self, code: str, days: int = 365,
                  adj: str = "qfq") -> list[KlineBar]:
        bars = self._klines(code, max(days, 800), adj=adj)
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

    def get_dividend_metric(self, code: str, latest_price: Optional[float] = None,
                            years: int = 10) -> Optional[DividendMetric]:
        """baostock 分红历史(query_dividend_data, 财年口径)→ DividendMetric。

        baostock 按「财年 yearType=report」查分红预案;每股税前现金分红字段
        dividCashPsBeforeTax 已是每股口径(茅台「10派308.76」→ 30.876),无需再 /10。
        遍历近 years 年覆盖跨年除权(年报分红常在次年除权)。免费替代 akshare 东财分红接口。

        字段序(实测 query_dividend_data 返回):
          0code 1preNotice 2agm 3planAnnounce 4planDate 5registDate
          6operateDate(除权除息日) 7payDate 8stockMktDate
          9cashPsBeforeTax(每股税前) 10cashPsAfterTax 11stocksPs 12cashStockText 13reserveToStock
        """
        if not self._ensure_login():
            return None
        import baostock as bs
        from datetime import timedelta

        this_year = date.today().year
        events: list[DividendEventDTO] = []
        empty_year0 = False   # 首年即空 → 可能掉线,触发一次 force_relogin
        for y in range(this_year - years + 1, this_year + 1):
            try:
                rs = bs.query_dividend_data(_bs_code(code), year=y, yearType="report")
            except Exception:  # noqa: BLE001
                continue
            err = getattr(rs, "error_code", "1")
            if err != "0":
                # 非"无数据"的真实错误(常为掉线):首年失败时重连一次再试
                if y == this_year - years + 1 and not empty_year0:
                    empty_year0 = True
                    self.force_relogin()
                    try:
                        rs = bs.query_dividend_data(_bs_code(code), year=y, yearType="report")
                    except Exception:  # noqa: BLE001
                        continue
                    if getattr(rs, "error_code", "1") != "0":
                        continue
                else:
                    continue
            while rs.next():
                row = rs.get_row_data()
                ex = _parse_date(row[6]) if len(row) > 6 else None
                cash = _f(row[9]) if len(row) > 9 else None
                if ex is None or not cash or cash <= 0:
                    continue   # 送转股无现金 / 未实施 / 字段缺失
                # 防 baostock 结果集串行 bleed：某财年查询返回了别年的陈旧行（实测出现过 2017 标签配 2026 ex_date / 28元）。
                # 正常分红 ex_date 年与财年相差 0~1；偏差 ≥2 视为脏数据丢弃，否则陈旧行会灌进 TTM 虚高股息率。
                if abs(ex.year - y) > 1:
                    continue
                events.append(DividendEventDTO(
                    ex_date=ex, per_share_cash=cash,
                    record_date=_parse_date(row[5]) if len(row) > 5 else None,
                    announce_date=_parse_date(row[3]) if len(row) > 3 else None,
                    currency="CNY",
                    source=f"baostock:dividend/{y}",
                ))
        if not events:
            return None
        # TTM 近 365 天每股现金分红(算子层会按 as_of 重算,此处仅展示用)
        ref = date.today()
        ttm = sum(e.per_share_cash for e in events
                  if ref - timedelta(days=365) <= e.ex_date <= ref)   # 上下界：排除 future ex_date（防未来函数）
        ttm_yield = (round(ttm / latest_price * 100, 2)
                     if latest_price and latest_price > 0 else None)
        return DividendMetric(
            code=code, currency="CNY",
            ttm_cash_per_share=round(ttm, 4),
            ttm_yield_pct=ttm_yield,
            events=events,
            coverage=f"baostock:{this_year - years + 1}-{this_year}({len(events)}次)",
        )


def get_index_daily_baostock(symbol: str, start: str, end: str) -> list[dict]:
    """baostock 指数日线（akshare 不可用时的 fallback，供回测基准更新用）。

    symbol: 指数代码，如 "000001"（上证综指）。start/end: "YYYY-MM-DD"。
    返回 list[dict]，结构与 akshare_source.get_index_daily 一致：
    asset_code=f"sh{symbol}"（399xxx → sz）、quote_date/open/high/low/close/pct_chg/volume/amount。
    baostock 未装/未登录/查询失败 → 返回 []。
    """
    if not BaostockSource._ensure_login():
        return []
    import baostock as bs
    try:
        rs = bs.query_history_k_data_plus(
            _index_bs_symbol(symbol),
            "date,open,high,low,close,pctChg,volume,amount",
            start_date=start, end_date=end, frequency="d")
    except Exception:  # noqa: BLE001
        return []
    asset_code = ("sz" if symbol.startswith("399") else "sh") + symbol
    results: list[dict] = []
    while getattr(rs, "error_code", "1") == "0" and rs.next():
        row = rs.get_row_data()
        try:
            d = datetime.strptime(row[0], "%Y-%m-%d").date()
            close_val = _f(row[4])
            if close_val is None:
                continue
            results.append({
                "asset_code": asset_code,
                "quote_date": d,
                "open": _f(row[1]), "high": _f(row[2]), "low": _f(row[3]),
                "close": close_val,
                "pct_chg": _f(row[5]),
                "volume": _f(row[6]), "amount": _f(row[7]),
            })
        except (KeyError, ValueError, TypeError, IndexError):
            continue
    return results
