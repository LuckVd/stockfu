"""股息/分红服务：薄封装数据层，并负责把分红事件落库(按 ex_date 去重)。

读路径默认 **优先本地 dividend_event 表**（邮件/看板不再为 TTM 股息打 baostock）。
写路径 ``persist_dividends`` 强制联网刷新后落库。
"""
from __future__ import annotations

from datetime import date, timedelta
import math
from dataclasses import dataclass
from typing import Iterable, Optional

from sqlmodel import select

from stockfu.data.base import DividendEventDTO, DividendMetric
from stockfu.data.manager import get_manager
from stockfu.db import session_scope
from stockfu.models import DividendEvent


# 回测分红供给器：engine 每次 run 批量预载 dividend_event 后挂载。
# fn(code, start, ref) -> list[(ex_date, per_share_cash)] | None；None 回退 DB。
_BT_DIVIDEND_PROVIDER = None


class CorporateActionConflictError(ValueError):
    """同一股票同一除权日出现互相矛盾的公司行为，拒绝静默双记。"""


@dataclass(frozen=True)
class _DividendResolution:
    """经 BaoStock 原始字段审计后的单日分红裁决。

    研究模式的 ``dividend_event`` 以 ``(code, ex_date)`` 表示一个可供 TTM
    使用的现金流总额，无法保留同日多笔事件的独立身份。因此只对已人工审计的
    极少数历史异常做显式归并；新异常绝不能被这个表静默吞掉。
    """

    final: DividendEventDTO
    expected: tuple[DividendEventDTO, ...]


def _event_signature(event: DividendEventDTO) -> tuple:
    """比较来源候选的稳定字段，浮点按 BaoStock 最多 7 位小数归一。"""
    return (
        round(float(event.per_share_cash or 0.0), 7),
        round(float(event.per_share_stock or 0.0), 7),
        event.record_date,
        event.announce_date,
        event.pay_date,
        event.stock_mkt_date,
        (round(float(event.per_share_cash_after_tax), 7)
         if event.per_share_cash_after_tax is not None else None),
    )


def _dto(
    ex_date: date, cash: float, stock: float, record_date: date | None,
    announce_date: date | None, pay_date: date | None, stock_mkt_date: date | None,
    after_tax: float | None, source: str,
) -> DividendEventDTO:
    return DividendEventDTO(
        ex_date=ex_date, per_share_cash=cash, per_share_stock=stock,
        record_date=record_date, announce_date=announce_date, pay_date=pay_date,
        stock_mkt_date=stock_mkt_date, per_share_cash_after_tax=after_tax,
        currency="CNY", source=source,
    )


# 2026-07-28 BaoStock 单源逐字段审计结论。键只覆盖确有冲突的 10 个
# (证券, 除权日)；300315/2012-10-22 没有可信裁决，故意不在表中。
_KNOWN_DIVIDEND_RESOLUTIONS: dict[tuple[str, date], _DividendResolution] = {
    ("000738", date(2013, 6, 14)): _DividendResolution(
        _dto(date(2013, 6, 14), .062, 0, date(2013, 6, 13), date(2013, 5, 17),
             date(2013, 6, 14), None, .0558, "baostock:dividend/2013"),
        (_dto(date(2013, 6, 14), .021, 0, date(2013, 6, 13), date(2013, 3, 30),
              date(2013, 6, 14), None, .0189, "baostock:dividend/2013"),
         _dto(date(2013, 6, 14), .041, 0, date(2013, 6, 13), date(2013, 5, 17),
              date(2013, 6, 14), None, .0369, "baostock:dividend/2013")),
    ),
    ("002434", date(2018, 5, 22)): _DividendResolution(
        _dto(date(2018, 5, 22), .150224, 0, date(2018, 5, 21), date(2018, 4, 17),
             date(2018, 5, 22), None, .1352016, "baostock:dividend/2018"),
        (_dto(date(2018, 5, 22), .15, 0, date(2018, 5, 21), date(2018, 4, 17),
              date(2018, 5, 22), None, .135, "baostock:dividend/2018"),
         _dto(date(2018, 5, 22), .150224, 0, date(2018, 5, 21), date(2018, 4, 17),
              date(2018, 5, 22), None, .1352016, "baostock:dividend/2018")),
    ),
    ("002601", date(2018, 10, 25)): _DividendResolution(
        _dto(date(2018, 10, 25), .65, 0, date(2018, 10, 24), date(2018, 8, 21),
             date(2018, 10, 25), None, .585, "baostock:dividend/2018"),
        (_dto(date(2018, 10, 25), .65, 0, date(2018, 10, 24), date(2018, 8, 21),
              date(2018, 10, 25), None, .585, "baostock:dividend/2018"),
         _dto(date(2018, 10, 25), .654028, 0, date(2018, 10, 24), date(2018, 8, 21),
              date(2018, 10, 25), None, .5886251, "baostock:dividend/2018")),
    ),
    ("300014", date(2018, 6, 8)): _DividendResolution(
        _dto(date(2018, 6, 8), .1, 0, date(2018, 6, 7), date(2018, 3, 20),
             date(2018, 6, 8), None, .09, "baostock:dividend/2018"),
        (_dto(date(2018, 6, 8), .1, 0, date(2018, 6, 7), date(2018, 3, 20),
              date(2018, 6, 8), None, .09, "baostock:dividend/2018"),
         _dto(date(2018, 6, 8), .100104, 0, date(2018, 6, 7), date(2018, 3, 20),
              date(2018, 6, 8), None, .0900932, "baostock:dividend/2018")),
    ),
    ("300015", date(2021, 6, 24)): _DividendResolution(
        _dto(date(2021, 6, 24), .148289, .296579, date(2021, 6, 23), date(2021, 4, 23),
             date(2021, 6, 24), date(2021, 6, 24), .1334604, "baostock:dividend/2021"),
        (_dto(date(2021, 6, 24), .148289, 0, date(2021, 6, 23), date(2021, 4, 23),
              date(2021, 6, 24), date(2021, 6, 24), .1334604, "baostock:dividend/2021"),
         _dto(date(2021, 6, 24), .148289, .296579, date(2021, 6, 23), date(2021, 4, 23),
              date(2021, 6, 24), date(2021, 6, 24), .1334604, "baostock:dividend/2021")),
    ),
    ("300498", date(2018, 5, 31)): _DividendResolution(
        _dto(date(2018, 5, 31), .4, 0, date(2018, 5, 30), date(2018, 4, 10),
             date(2018, 5, 31), None, .36, "baostock:dividend/2018"),
        (_dto(date(2018, 5, 31), .4, 0, date(2018, 5, 30), date(2018, 4, 10),
              date(2018, 5, 31), None, .36, "baostock:dividend/2018"),
         _dto(date(2018, 5, 31), .392963, 0, date(2018, 5, 30), date(2018, 4, 10),
              date(2018, 5, 31), None, .3536667, "baostock:dividend/2018")),
    ),
    ("300760", date(2025, 5, 29)): _DividendResolution(
        _dto(date(2025, 5, 29), 1.97, 0, date(2025, 5, 28), date(2025, 4, 29),
             date(2025, 5, 29), None, 1.773, "baostock:dividend/2025"),
        (_dto(date(2025, 5, 29), .56, 0, date(2025, 5, 28), date(2025, 4, 29),
              date(2025, 5, 29), None, .504, "baostock:dividend/2025"),
         _dto(date(2025, 5, 29), 1.41, 0, date(2025, 5, 28), date(2025, 4, 29),
              date(2025, 5, 29), None, 1.269, "baostock:dividend/2025")),
    ),
    ("301308", date(2026, 6, 2)): _DividendResolution(
        _dto(date(2026, 6, 2), .990744, 0, date(2026, 6, 1), date(2026, 4, 28),
             date(2026, 6, 2), None, .8916698, "baostock:dividend/2026"),
        (_dto(date(2026, 6, 2), .34676, 0, date(2026, 6, 1), date(2026, 4, 28),
              date(2026, 6, 2), None, .3120845, "baostock:dividend/2026"),
         _dto(date(2026, 6, 2), .643984, 0, date(2026, 6, 1), date(2026, 4, 28),
              date(2026, 6, 2), None, .5795853, "baostock:dividend/2026")),
    ),
    ("600989", date(2019, 9, 27)): _DividendResolution(
        _dto(date(2019, 9, 27), .59636, 0, date(2019, 9, 26), date(2019, 8, 9),
             date(2019, 9, 27), None, .536724, "baostock:dividend/2019"),
        (_dto(date(2019, 9, 27), .32091, 0, date(2019, 9, 26), date(2019, 8, 9),
              date(2019, 9, 27), None, .288819, "baostock:dividend/2019"),
         _dto(date(2019, 9, 27), .27545, 0, date(2019, 9, 26), date(2019, 8, 9),
              date(2019, 9, 27), None, .247905, "baostock:dividend/2019")),
    ),
    ("601966", date(2024, 6, 14)): _DividendResolution(
        _dto(date(2024, 6, 14), .377, 0, date(2024, 6, 13), date(2024, 4, 25),
             date(2024, 6, 14), None, .3393, "baostock:dividend/2024"),
        (_dto(date(2024, 6, 14), .286, 0, date(2024, 6, 13), date(2024, 4, 25),
              date(2024, 6, 14), None, .2574, "baostock:dividend/2024"),
         _dto(date(2024, 6, 14), .091, 0, date(2024, 6, 13), date(2024, 4, 25),
              date(2024, 6, 14), None, .0819, "baostock:dividend/2024")),
    ),
}


def _summarize_corporate_action_rows(
    rows: Iterable[DividendEvent], *, start_year: int, end_year: int,
) -> dict:
    """将公司行为行转换为可提交、可比较的只读质量报告。"""
    years = {
        str(year): {"events": 0, "assets": set(), "cash_events": 0, "stock_events": 0}
        for year in range(start_year, end_year + 1)
    }
    duplicate_groups: dict[tuple[str, date], int] = {}
    invalid: list[dict] = []
    total = 0
    assets: set[str] = set()
    for row in rows:
        total += 1
        assets.add(row.asset_code)
        duplicate_groups[(row.asset_code, row.ex_date)] = duplicate_groups.get(
            (row.asset_code, row.ex_date), 0
        ) + 1
        cash, stock = float(row.per_share_cash or 0.0), float(row.per_share_stock or 0.0)
        if cash < 0 or stock < 0 or (cash == 0 and stock == 0):
            invalid.append({"code": row.asset_code, "ex_date": row.ex_date.isoformat(),
                            "cash": cash, "stock": stock, "reason": "invalid_amount"})
        if not row.source:
            invalid.append({"code": row.asset_code, "ex_date": row.ex_date.isoformat(),
                            "reason": "missing_source"})
        bucket = years.get(str(row.ex_date.year))
        if bucket is not None:
            bucket["events"] += 1
            bucket["assets"].add(row.asset_code)
            bucket["cash_events"] += int(cash > 0)
            bucket["stock_events"] += int(stock > 0)
    by_year = {
        year: {**bucket, "assets": len(bucket["assets"])} for year, bucket in years.items()
    }
    duplicates = [
        {"code": code, "ex_date": ex_date.isoformat(), "count": count}
        for (code, ex_date), count in sorted(duplicate_groups.items()) if count > 1
    ]
    return {
        "window": {"start_year": start_year, "end_year": end_year},
        "events": total,
        "assets": len(assets),
        "by_year": by_year,
        "zero_event_years": [year for year, bucket in by_year.items() if bucket["events"] == 0],
        "duplicate_groups": duplicates,
        "invalid_rows": invalid,
        "ready_for_formal_backtest": not duplicates and not invalid
        and not any(bucket["events"] == 0 for bucket in by_year.values()),
    }


def audit_corporate_actions(*, start_year: int = 2007, end_year: int | None = None) -> dict:
    """只读审计 ``dividend_event``，绝不把“没有事件”解释为“没有公司行为”。"""
    end_year = end_year or date.today().year
    if end_year < start_year:
        raise ValueError("end_year 不能早于 start_year")
    with session_scope() as s:
        rows = s.exec(select(DividendEvent).where(
            DividendEvent.ex_date >= date(start_year, 1, 1),
            DividendEvent.ex_date <= date(end_year, 12, 31),
        )).all()
    return _summarize_corporate_action_rows(rows, start_year=start_year, end_year=end_year)


def _canonical_events(events: list[DividendEventDTO], *, code: str | None = None) -> list[DividendEventDTO]:
    """按除权日折叠完全相同的源重复；数值/登记日冲突必须人工裁决。

    回测会在除权日逐条结算，不能把两个互相矛盾的供应商行相加。实际存在多条
    同日公司行为时也必须提供更细粒度的官方事件键后才能放行，避免把数据瑕疵
    误当收益。
    """
    by_date: dict[date, list[DividendEventDTO]] = {}
    for event in events:
        by_date.setdefault(event.ex_date, []).append(event)

    out: dict[date, DividendEventDTO] = {}
    for ex_date, group in by_date.items():
        resolution = _KNOWN_DIVIDEND_RESOLUTIONS.get((code or "", ex_date))
        if resolution is not None:
            actual = sorted(_event_signature(event) for event in group)
            expected = sorted(_event_signature(event) for event in resolution.expected)
            if actual != expected:
                raise CorporateActionConflictError(
                    f"{code} {ex_date}: 已裁决事件的来源候选发生变化，拒绝静默覆盖"
                )
            out[ex_date] = resolution.final
            continue

        old = group[0]
        for event in group[1:]:
            same_amount = (
                math.isclose(float(old.per_share_cash or 0), float(event.per_share_cash or 0),
                             abs_tol=1e-8)
                and math.isclose(float(old.per_share_stock or 0), float(event.per_share_stock or 0),
                                 abs_tol=1e-8)
            )
            same_dates = old.record_date == event.record_date and old.announce_date == event.announce_date
            if not (same_amount and same_dates):
                raise CorporateActionConflictError(
                    f"{code + ' ' if code else ''}{ex_date}: 同一除权日存在冲突公司行为 "
                    f"({old.per_share_cash}/{old.per_share_stock} vs "
                    f"{event.per_share_cash}/{event.per_share_stock})"
                )
        out[ex_date] = old
    return [out[d] for d in sorted(out)]


def _event_matches_dto(row: DividendEvent, dto: DividendEventDTO) -> bool:
    """判断本地行是否已是裁决后的最终值，供修复命令幂等重跑。"""
    return (
        math.isclose(float(row.per_share_cash or 0), float(dto.per_share_cash or 0), abs_tol=1e-8)
        and math.isclose(float(row.per_share_stock or 0), float(dto.per_share_stock or 0), abs_tol=1e-8)
        and row.record_date == dto.record_date and row.announce_date == dto.announce_date
        and row.pay_date == dto.pay_date and row.stock_mkt_date == dto.stock_mkt_date
        and row.per_share_cash_after_tax == dto.per_share_cash_after_tax
        and row.currency == dto.currency and row.source == dto.source
    )


def _repair_known_dividend_conflicts_in_session(
    s, resolutions: dict[tuple[str, date], _DividendResolution] | None = None,
) -> dict[str, int]:
    """按人工审计表修复历史重复行；未知旧值或额外行一律拒绝写入。"""
    summary = {"inserted": 0, "updated": 0, "deleted": 0, "unchanged": 0}
    for (code, ex_date), resolution in (resolutions or _KNOWN_DIVIDEND_RESOLUTIONS).items():
        rows = list(s.exec(select(DividendEvent).where(
            DividendEvent.asset_code == code, DividendEvent.ex_date == ex_date,
        )).all())
        final = resolution.final
        expected_cash = {round(float(event.per_share_cash or 0), 7) for event in resolution.expected}
        final_cash = round(float(final.per_share_cash or 0), 7)
        unknown = [row for row in rows if round(float(row.per_share_cash or 0), 7)
                   not in expected_cash | {final_cash}]
        if unknown:
            raise CorporateActionConflictError(
                f"{code} {ex_date}: 本地存在未审计金额，拒绝自动修复"
            )
        if len(rows) == 1 and _event_matches_dto(rows[0], final):
            summary["unchanged"] += 1
            continue
        if rows:
            primary = min(rows, key=lambda row: row.id or 0)
            for row in rows:
                if row is not primary:
                    s.delete(row)
                    summary["deleted"] += 1
        else:
            primary = DividendEvent(asset_code=code, ex_date=ex_date)
            s.add(primary)
            summary["inserted"] += 1
        before = _event_matches_dto(primary, final)
        primary.record_date = final.record_date
        primary.announce_date = final.announce_date
        primary.per_share_cash = float(final.per_share_cash or 0)
        primary.per_share_stock = float(final.per_share_stock or 0)
        primary.pay_date = final.pay_date
        primary.stock_mkt_date = final.stock_mkt_date
        primary.per_share_cash_after_tax = final.per_share_cash_after_tax
        primary.currency = final.currency
        primary.source = final.source
        if not before and rows:
            summary["updated"] += 1
    return summary


def repair_known_dividend_conflicts() -> dict[str, int]:
    """事务化修复 10 个已审计分红冲突；300315 保持失败，等待人工公告核验。"""
    with session_scope() as s:
        summary = _repair_known_dividend_conflicts_in_session(s)
        s.commit()
    return summary


def set_backtest_dividend_provider(fn) -> None:
    """挂载回测 TTM 分红内存供给器。"""
    global _BT_DIVIDEND_PROVIDER
    _BT_DIVIDEND_PROVIDER = fn


def clear_backtest_dividend_provider() -> None:
    """摘除回测分红供给器，恢复 live 数据库读取。"""
    global _BT_DIVIDEND_PROVIDER
    _BT_DIVIDEND_PROVIDER = None


def metric_from_db(
    code: str,
    latest_price: Optional[float] = None,
    *,
    years: int = 10,
) -> Optional[DividendMetric]:
    """从 ``dividend_event`` 表组装 DividendMetric；无事件返回 None。

    TTM = 近 365 天每股现金分红合计；股息率 = TTM / latest_price × 100（有价时）。
    """
    this_year = date.today().year
    start_year = this_year - years + 1
    # 覆盖跨年除权：取 start_year-01-01 起全部事件
    start_floor = date(start_year, 1, 1)
    with session_scope() as s:
        rows = s.exec(
            select(DividendEvent)
            .where(
                DividendEvent.asset_code == code,
                DividendEvent.ex_date >= start_floor,
            )
            .order_by(DividendEvent.ex_date)
        ).all()
    if not rows:
        return None
    events = [
        DividendEventDTO(
            ex_date=r.ex_date,
            per_share_cash=float(r.per_share_cash or 0),
            per_share_stock=float(r.per_share_stock or 0),
            record_date=r.record_date,
            announce_date=r.announce_date,
            currency=r.currency or "CNY",
            source=r.source or "db:dividend_event",
        )
        for r in rows
        if r.ex_date and r.per_share_cash and float(r.per_share_cash) > 0
    ]
    if not events:
        return None
    ref = date.today()
    ttm = sum(
        e.per_share_cash for e in events
        if ref - timedelta(days=365) <= e.ex_date <= ref   # 上下界：排除未除权的 future ex_date（防未来函数）
    )
    ttm_yield = (
        round(ttm / latest_price * 100, 2)
        if latest_price and latest_price > 0 and ttm > 0
        else None
    )
    cur = events[-1].currency if events else "CNY"
    return DividendMetric(
        code=code,
        currency=cur,
        ttm_cash_per_share=round(ttm, 4),
        ttm_yield_pct=ttm_yield,
        events=events,
        coverage=f"db:{start_year}-{this_year}({len(events)}次)",
    )


def persist_dividends(code: str, *, years: int = 10, timeout: float = 10.0) -> int:
    """拉取公司行为并按除权日 upsert；冲突事件拒绝静默写入。

    强制联网（force_network），不走「仅库」短路，否则无法回补。
    """
    metric = get_manager().get_dividend_metric(
        code, force_network=True, years=years, timeout=timeout,
    )
    if not metric or not metric.events:
        return 0
    incoming = _canonical_events(metric.events, code=code)
    written = 0
    with session_scope() as s:
        existing_by_date: dict[date, list[DividendEvent]] = {}
        for row in s.exec(select(DividendEvent).where(DividendEvent.asset_code == code)).all():
            existing_by_date.setdefault(row.ex_date, []).append(row)
        for e in incoming:
            rows = existing_by_date.get(e.ex_date, [])
            if len(rows) > 1:
                raise CorporateActionConflictError(
                    f"{code} {e.ex_date}: 本地已有 {len(rows)} 条事件，须先裁决后回灌"
                )
            if rows:
                row = rows[0]
                before = (row.record_date, row.announce_date, row.per_share_cash,
                          row.per_share_stock, row.pay_date, row.stock_mkt_date,
                          row.per_share_cash_after_tax, row.currency, row.source)
                row.record_date = e.record_date
                row.announce_date = e.announce_date
                row.per_share_cash = float(e.per_share_cash or 0)
                row.per_share_stock = float(e.per_share_stock or 0)
                row.pay_date = e.pay_date
                row.stock_mkt_date = e.stock_mkt_date
                row.per_share_cash_after_tax = e.per_share_cash_after_tax
                row.currency = e.currency
                row.source = e.source
                after = (row.record_date, row.announce_date, row.per_share_cash,
                         row.per_share_stock, row.pay_date, row.stock_mkt_date,
                         row.per_share_cash_after_tax, row.currency, row.source)
                written += int(before != after)
            else:
                s.add(DividendEvent(
                    asset_code=code, ex_date=e.ex_date,
                    record_date=e.record_date, announce_date=e.announce_date,
                    pay_date=e.pay_date, stock_mkt_date=e.stock_mkt_date,
                    per_share_cash=float(e.per_share_cash or 0),
                    per_share_stock=float(e.per_share_stock or 0),
                    per_share_cash_after_tax=e.per_share_cash_after_tax,
                    currency=e.currency, source=e.source,
                ))
                written += 1
        s.commit()
    return written


def dividend_yield_ttm(code: str, as_of=None) -> tuple[float, float] | None:
    """as_of 日 TTM 股息率(%):近 365 天每股现金分红 / as_of 日**不复权**收盘价 × 100。

    分红来自 dividend_event 表(baostock query_dividend_data / akshare 回补);
    **分母必须用 close_raw(不复权)**:名义现金 ÷ 全样本前复权价会虚高并引入 qfq 前视。
    严格 ex_date <= as_of 防未来函数。
    返回 (yield_pct, ttm_per_share) 或 None(无分红/无价/未回补 close_raw)。
    """
    from stockfu.services.factors import ADJ_RAW, quote_series

    ref = as_of or date.today()
    year_ago = ref - timedelta(days=365)
    rows: list[tuple[date, float | None]] | None = None
    if _BT_DIVIDEND_PROVIDER is not None:
        rows = _BT_DIVIDEND_PROVIDER(code, year_ago, ref)
    if rows is None:
        with session_scope() as s:
            db_rows = s.exec(select(DividendEvent).where(
                DividendEvent.asset_code == code,
                DividendEvent.ex_date <= ref,
                DividendEvent.ex_date >= year_ago,
            )).all()
        rows = [(r.ex_date, r.per_share_cash) for r in db_rows]
    if not rows:
        return None
    ttm = sum(cash for _ex_date, cash in rows if cash and cash > 0)
    if ttm <= 0:
        return None
    # 不复权收盘;未回补 raw 时不回落 qfq(避免静默污染)
    closes = quote_series(code, "close", 10, as_of=ref, adj=ADJ_RAW)
    if not closes or closes[-1] <= 0:
        return None
    return round(ttm / closes[-1] * 100, 3), round(ttm, 4)
