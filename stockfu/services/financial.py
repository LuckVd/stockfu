"""财务三表 PIT 查询服务（东财 financial_profit / financial_balance / financial_cashflow）。

PIT 语义（docs/SPECS/financial-data-design.md §2.2）：某交易日 as_of → 该股票在此日前
**最新已公告**的财报（pub_date = NOTICE_DATE 公告日 <= as_of），禁止用 stat_date 过滤
（报告期披露有滞后，用报告期会引入未来函数）。

字段级可见性：三表公告日可能不同（实测：茅台 balance 2025Q4 公告日 04-25 晚于
profit 04-17），跨表合成因子（如 GPOA=毛利/总资产）必须要求**每个来源字段所在表
都已公告**（FinancialReport.visible），否则保守缺失，绝不混用不同报告期拼凑。

两种路径：
- live/未预载：直接查 SQLite（三表），逐次查询。
- 回测：由 backtest.engine 预载全部宇宙财务行到内存并挂 provider（_BT_FINANCIAL_PROVIDER），
  零 DB、按日过滤，供 quality 等 raw 因子逐 (code, as_of) 调用。

本模块只提供"取数 + PIT 过滤"，不做因子计算（那是 factors/raw 层职责）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

from sqlmodel import select

from stockfu.db import session_scope
from stockfu.models import FinancialBalance, FinancialCashflow, FinancialProfit

# 回测财务供给器：engine 预载后挂载，避免 quality 因子对每个 (code, as_of)
# 都开一次 session 查询多年财务序列。
# fn(code, as_of) -> list[FinancialReport] | None（该股票全部报告，按 (year, quarter) 降序）。
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


# 字段 → 来源表公告日属性名（跨表因子 PIT 的依据）
_FIELD_PUB = {
    "roe_avg": "pub_profit", "gp_margin": "pub_profit", "net_profit": "pub_profit",
    "revenue": "pub_profit",
    "revenue_yoy": "pub_profit", "net_profit_yoy": "pub_profit",
    "total_assets": "pub_balance", "liability_to_asset": "pub_balance",
    "equity": "pub_balance",
    "net_cash_oper": "pub_cashflow",
}


@dataclass(frozen=True)
class FinancialReport:
    """一张报告期（三表合并）的最小 PIT 视图。

    同一 (year, quarter) 三表合并为一行；分表公告日分别记录，字段级可见性
    由 visible() 判定。pub_date 属性 = 最早公告日，仅作诊断/排序参考，
    **不得**用作跨表因子的 PIT 依据。
    """

    year: int
    quarter: int = 0      # 1-4（4=年报）
    stat_date: date | None = None
    pub_profit: date | None = None    # profit 表公告日
    pub_balance: date | None = None   # balance 表公告日
    pub_cashflow: date | None = None  # cashflow 表公告日
    roe_avg: float | None = None      # 净资产收益率%（WEIGHTAVG_ROE）
    gp_margin: float | None = None    # 销售毛利率%（XSMLL）
    net_profit: float | None = None   # 归母净利润（元，PARENT_NETPROFIT）
    revenue: float | None = None      # 营业总收入（元，TOTAL_OPERATE_INCOME）
    revenue_yoy: float | None = None  # 营收同比（%，YSTZ）
    net_profit_yoy: float | None = None  # 净利同比（%，SJLTZ）
    total_assets: float | None = None # 总资产（元，TOTAL_ASSETS）
    liability_to_asset: float | None = None  # 资产负债率%（LIABILITY_TO_ASSET）
    equity: float | None = None       # 股东权益合计（元，TOTAL_EQUITY）
    net_cash_oper: float | None = None  # 经营现金流净额（元，NETCASH_OPERATE）

    @property
    def pub_date(self) -> date | None:
        """最早公告日（诊断/排序参考，非 PIT 依据）。"""
        pubs = [p for p in (self.pub_profit, self.pub_balance, self.pub_cashflow)
                if p is not None]
        return min(pubs) if pubs else None

    def visible(self, field: str, as_of: date) -> bool:
        """字段在 as_of 是否已公开：该字段来源表的公告日 <= as_of。"""
        pub = getattr(self, _FIELD_PUB.get(field, ""), None)
        return pub is not None and pub <= as_of

    def visible_all(self, fields: tuple[str, ...], as_of: date) -> bool:
        return all(self.visible(f, as_of) for f in fields)


def _rows_to_reports(rows: list[Any]) -> list[FinancialReport]:
    """ORM 行（三表混合）→ 财务视图：按 (year, quarter) 合并为一行。

    profit 行带 roe/gp/net_profit/revenue，balance 行带 total_assets/liability/equity，
    cashflow 行带 net_cash_oper；同报告期三行字段互补合并，分表公告日分别记录。
    返回按 (year, quarter) 降序（最新报告期在前）。
    """
    merged: dict[tuple[int, int], dict[str, Any]] = {}
    for r in rows:
        key = (r.year, r.quarter)
        slot = merged.setdefault(key, {})
        for attr in ("pub_profit", "pub_balance", "pub_cashflow"):
            if getattr(r, "pub_date", None) is not None:
                table = type(r).__tablename__ if hasattr(type(r), "__tablename__") else ""
                src = {"financial_profit": "pub_profit",
                       "financial_balance": "pub_balance",
                       "financial_cashflow": "pub_cashflow"}.get(table)
                if src and slot.get(src) is None:
                    slot[src] = r.pub_date
        for f in ("roe_avg", "gp_margin", "net_profit", "revenue",
                  "revenue_yoy", "net_profit_yoy",
                  "total_assets", "liability_to_asset", "equity",
                  "net_cash_oper"):
            v = getattr(r, f, None)
            if v is not None and slot.get(f) is None:
                slot[f] = float(v)
        if slot.get("stat_date") is None and getattr(r, "stat_date", None) is not None:
            slot["stat_date"] = r.stat_date
    out = []
    for (year, quarter), slot in merged.items():
        out.append(FinancialReport(
            year=year, quarter=quarter,
            stat_date=slot.get("stat_date"),
            pub_profit=slot.get("pub_profit"),
            pub_balance=slot.get("pub_balance"),
            pub_cashflow=slot.get("pub_cashflow"),
            roe_avg=slot.get("roe_avg"), gp_margin=slot.get("gp_margin"),
            net_profit=slot.get("net_profit"), revenue=slot.get("revenue"),
            revenue_yoy=slot.get("revenue_yoy"),
            net_profit_yoy=slot.get("net_profit_yoy"),
            total_assets=slot.get("total_assets"),
            liability_to_asset=slot.get("liability_to_asset"),
            equity=slot.get("equity"), net_cash_oper=slot.get("net_cash_oper"),
        ))
    out.sort(key=lambda x: (x.year, x.quarter), reverse=True)
    return out


def financial_reports(code: str, as_of: date) -> list[FinancialReport] | None:
    """该股票全部报告（(year, quarter) 降序）；无数据 → []。

    先走回测 provider（预载内存）；未预载回落 DB 一次查询三表。
    as_of 仅用于 provider 切片（预载数据可能含未来报告，由调用方 visible 过滤）。
    """
    if _BT_FINANCIAL_PROVIDER is not None:
        rows = _BT_FINANCIAL_PROVIDER(code, as_of)
        if rows is not None:
            return rows
    with session_scope() as s:
        p_rows = s.exec(select(FinancialProfit).where(
            FinancialProfit.asset_code == code)).all()
        b_rows = s.exec(select(FinancialBalance).where(
            FinancialBalance.asset_code == code)).all()
        c_rows = s.exec(select(FinancialCashflow).where(
            FinancialCashflow.asset_code == code)).all()
    return _rows_to_reports(list(p_rows) + list(b_rows) + list(c_rows))


def latest_financial_report(code: str, as_of: date,
                            *, require: tuple[str, ...] = (),
                            quarters: tuple[int, ...] | None = None,
                            table: str | None = None) -> FinancialReport | None:
    """PIT 最新已公告报告期：按 (year, quarter) 降序，第一个满足可见性约束的行。

    - require：该行所有字段来源表都已在 as_of 前公告（跨表合成因子必用）。
    - quarters：只允许这些报告期（如 (4,) 只取年报）。
    - table：旧兼容参数——"profit"→require=(roe_avg,gp_margin) 之一可见？
      新语义改为字段级：table="profit" 等价 require=(roe_avg,)，见调用方。
    """
    rows = financial_reports(code, as_of) or []
    if quarters is not None:
        qs = set(quarters)
        rows = [r for r in rows if r.quarter in qs]
    req = tuple(require)
    if table == "profit":
        req = req + ("roe_avg",)
    elif table == "balance":
        req = req + ("liability_to_asset",)
    for r in rows:
        if req and not r.visible_all(req, as_of):
            continue
        return r
    return None


def financial_reports_before(code: str, as_of: date,
                             *, table: str = "profit",
                             quarters: tuple[int, ...] | None = None) -> list[FinancialReport]:
    """截至 as_of 已公告的财报子集（供 ROE 稳定性等序列聚合）。

    table: "profit"（要求 roe_avg 可见）| "balance"（liability 可见）|
    "any"（最早公告日 <= as_of）。quarters 只保留指定报告期。
    """
    rows = financial_reports(code, as_of) or []
    if quarters is not None:
        qs = set(quarters)
        rows = [r for r in rows if r.quarter in qs]
    if table == "profit":
        rows = [r for r in rows if r.visible("roe_avg", as_of)]
    elif table == "balance":
        rows = [r for r in rows if r.visible("liability_to_asset", as_of)]
    else:
        rows = [r for r in rows if r.pub_date is not None and r.pub_date <= as_of]
    return rows
