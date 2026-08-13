"""财务三表 PIT 查询服务（东财 financial_profit / financial_balance 等表）。

PIT 语义（docs/SPECS/financial-data-design.md §2.2）：某交易日 as_of → 该股票在此日前
**最新已公告**的财报（pub_date = NOTICE_DATE 公告日 <= as_of），禁止用 stat_date 过滤
（报告期披露有滞后，用报告期会引入未来函数）。

两种路径：
- live/未预载：直接查 SQLite（financial_profit / financial_balance），逐次一条索引查询。
- 回测：由 backtest.engine 预载全部宇宙财务行到内存并挂 provider（_BT_FINANCIAL_PROVIDER），
  零 DB、按日 bisect 切片，供 quality 等 raw 因子逐 (code, as_of) 调用。

本模块只提供"取数 + PIT 过滤"，不做因子计算（那是 factors/raw 层职责）。
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

from sqlmodel import select

from stockfu.db import session_scope
from stockfu.models import FinancialBalance, FinancialProfit

# 回测财务供给器：engine 预载后挂载，避免 quality 因子对每个 (code, as_of)
# 都开一次 session 查询多年财务序列。
# fn(code, as_of) -> list[FinancialReport] | None（该股票已公告报告，按 pub_date 升序）。
# None 表示不在回测预载范围内，必须回退 DB 以保持 live/边界调用正确。
_BT_FINANCIAL_PROVIDER: Callable | None = None


def set_backtest_financial_provider(fn: Callable) -> None:
    """挂载回测财务内存供给器（由 backtest.engine 生命周期管理）。"""
    global _BT_FINANCIAL_PROVIDER
    _BT_FINANCIAL_PROVIDER = fn


def clear_backtest_financial_provider() -> None:
    """摘除回测财务供给器，恢复 live 路径的数据库读取。"""
    global _BT_FINANCIAL_PROVIDER
    _BT_FINANCIAL_PROVIDER = None


@dataclass(frozen=True)
class FinancialReport:
    """一张财报的最小 PIT 视图（供因子计算，不含无关字段）。"""

    year: int
    quarter: int          # 1-4（4=年报）
    pub_date: date        # 公告日（PIT 过滤唯一依据）
    stat_date: date | None
    roe_avg: float | None        # 净资产收益率%（WEIGHTAVG_ROE）
    gp_margin: float | None      # 销售毛利率%（XSMLL）
    liability_to_asset: float | None   # 资产负债率%（LIABILITY_TO_ASSET，balance 表）


def _rows_to_reports(rows: list[Any]) -> list[FinancialReport]:
    """ORM 行 → 财务视图：同 (pub_date, year, quarter) 的 profit/balance 两行合并，
    按 (pub_date, year, quarter) 升序。

    profit 行带 roe_avg/gp_margin，balance 行带 liability_to_asset；同一报告期
    同日公告时字段互补合并（roe 取 profit 值、liability 取 balance 值），避免
    "最新"取到只有单表字段的行导致因子误缺失。
    """
    merged: dict[tuple[date, int, int], FinancialReport] = {}
    for r in rows:
        pub = r.pub_date
        if pub is None:
            continue        # 无公告日的行无法 PIT，宁可缺失
        key = (pub, r.year, r.quarter)
        old = merged.get(key)
        roe = (float(r.roe_avg)
               if getattr(r, "roe_avg", None) is not None else None)
        gp = (float(r.gp_margin)
              if getattr(r, "gp_margin", None) is not None else None)
        lia = (float(r.liability_to_asset)
               if getattr(r, "liability_to_asset", None) is not None else None)
        if old is None:
            merged[key] = FinancialReport(
                year=r.year, quarter=r.quarter, pub_date=pub, stat_date=r.stat_date,
                roe_avg=roe, gp_margin=gp, liability_to_asset=lia)
        else:
            merged[key] = FinancialReport(
                year=old.year, quarter=old.quarter, pub_date=pub,
                stat_date=old.stat_date or r.stat_date,
                roe_avg=roe if roe is not None else old.roe_avg,
                gp_margin=gp if gp is not None else old.gp_margin,
                liability_to_asset=lia if lia is not None else old.liability_to_asset)
    out = sorted(merged.values(), key=lambda x: (x.pub_date, x.year, x.quarter))
    return out


def financial_reports(code: str, as_of: date) -> list[FinancialReport] | None:
    """该股票截至 as_of 已公告的财报序列（pub_date 升序）；无数据 → []。

    先走回测 provider（预载内存）；未预载回落 DB 一次查询 profit + balance 两表。
    """
    if _BT_FINANCIAL_PROVIDER is not None:
        rows = _BT_FINANCIAL_PROVIDER(code, as_of)
        if rows is not None:
            return rows
    with session_scope() as s:
        p_rows = s.exec(select(FinancialProfit).where(
            FinancialProfit.asset_code == code,
        )).all()
        b_rows = s.exec(select(FinancialBalance).where(
            FinancialBalance.asset_code == code,
        )).all()
    merged = list(p_rows) + list(b_rows)
    return _rows_to_reports(merged)


def latest_financial_report(code: str, as_of: date) -> FinancialReport | None:
    """PIT 最新已公告财报：pub_date <= as_of 中 (pub_date, year, quarter) 最大的一行。

    无任何已公告报告 → None（由 raw 层按 missing 处理，不伪造）。
    """
    rows = financial_reports(code, as_of)
    if not rows:
        return None
    # 序列按 pub_date 升序；bisect 定位 <= as_of 的右边界，取最后一个。
    i = bisect_right([r.pub_date for r in rows], as_of)
    if i == 0:
        return None
    return rows[i - 1]


def financial_reports_before(code: str, as_of: date,
                             *, table: str = "profit",
                             quarters: tuple[int, ...] | None = None) -> list[FinancialReport]:
    """截至 as_of 已公告的财报子集（供 ROE 稳定性等序列聚合）。

    - table: "profit"（financial_profit 列优先）| "balance"（资产负债率列优先）|
      "any"（两表合并，同报告期去重保留先公告者）。
    - quarters: 只保留这些报告期（如 (4,) 取完整年度序列）。
    """
    rows = financial_reports(code, as_of) or []
    if quarters is not None:
        qs = set(quarters)
        rows = [r for r in rows if r.quarter in qs]
    if table == "profit":
        rows = [r for r in rows if r.roe_avg is not None or r.gp_margin is not None]
    elif table == "balance":
        rows = [r for r in rows if r.liability_to_asset is not None]
    return rows
