"""持仓汇总服务：把持仓(成本) × 实时行情 × 分红指标 → 看板视图。

输出每只持仓的市值/盈亏/股息率/年红利/回本进度，以及组合整体股息率与年红利收入。
注：MVP 不做币种换算，按数值汇总（多币种混算），每行保留各自 currency 供展示。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlmodel import select

from stockfu.data.manager import get_manager
from stockfu.db import session_scope
from stockfu.models import Asset, Holding, IndexSnapshot


@dataclass
class PositionView:
    code: str
    name: str = ""
    market: str = ""
    currency: str = "CNY"
    shares: float = 0.0
    avg_cost: float = 0.0
    price: float = 0.0
    market_value: float = 0.0
    cost: float = 0.0
    profit: float = 0.0
    profit_pct: float = 0.0
    ttm_yield_pct: float | None = None
    annual_dividend: float = 0.0      # 近 12 个月红利 ≈ 年红利
    recovered_pct: float = 0.0         # 年红利 / 成本 ×100（每年回本%）
    payback_years: float | None = None  # 成本 / 年红利（回本年限）
    # 个股三层情绪指数（index_snapshot level=stock，由 --fetch 或「加个股」自动算）
    fear: float | None = None
    greed: float | None = None
    heat: float | None = None


@dataclass
class PortfolioSummary:
    positions: list[PositionView] = field(default_factory=list)
    total_cost: float = 0.0
    total_value: float = 0.0
    total_profit: float = 0.0
    blended_yield_pct: float = 0.0     # 整体股息率 = 年红利 / 总市值
    annual_dividend_income: float = 0.0
    as_of: date = field(default_factory=date.today)
    mixed_currency: bool = False


def get_portfolio() -> PortfolioSummary:
    mgr = get_manager()
    positions: list[PositionView] = []
    currencies: set[str] = set()

    with session_scope() as s:
        holdings = s.exec(select(Holding)).all()
        assets = {a.code: a for a in s.exec(select(Asset)).all()}
        # 预读每只持仓股最新的 stock 层 fear/greed/heat（--fetch / 加个股 落库）
        stock_idx: dict[str, dict[str, float]] = {}
        if holdings:
            idx_rows = s.exec(select(IndexSnapshot).where(
                IndexSnapshot.level == "stock",
                IndexSnapshot.scope.in_([h.asset_code for h in holdings]),
            ).order_by(IndexSnapshot.scope, IndexSnapshot.snap_date.desc())).all()
            for r in idx_rows:
                d = stock_idx.setdefault(r.scope, {})
                if r.index_key in ("fear", "greed", "heat") and r.index_key not in d:
                    d[r.index_key] = r.value  # 已按日期降序，首个即最新
        for h in holdings:
            if h.shares <= 0:
                continue
            a = assets.get(h.asset_code)
            q = mgr.get_quote(h.asset_code)
            price = q.price if q else 0.0
            mv = price * h.shares
            cost = h.total_cost or (h.avg_cost * h.shares)
            profit = mv - cost
            m = mgr.get_dividend_metric(h.asset_code, latest_price=price) if q else None
            annual_div = (m.ttm_cash_per_share or 0.0) * h.shares
            cur = (a.currency if a else "") or (q.currency if q else "CNY")
            currencies.add(cur)
            positions.append(PositionView(
                code=h.asset_code,
                name=(a.name if a and a.name else (q.name if q else "")),
                market=a.market if a else "",
                currency=cur,
                shares=h.shares, avg_cost=h.avg_cost, price=price,
                market_value=mv, cost=cost, profit=profit,
                profit_pct=(profit / cost * 100) if cost else 0.0,
                ttm_yield_pct=(m.ttm_yield_pct if m else None),
                annual_dividend=annual_div,
                recovered_pct=(annual_div / cost * 100) if cost else 0.0,
                payback_years=(cost / annual_div) if annual_div > 0 else None,
                fear=stock_idx.get(h.asset_code, {}).get("fear"),
                greed=stock_idx.get(h.asset_code, {}).get("greed"),
                heat=stock_idx.get(h.asset_code, {}).get("heat"),
            ))

    positions.sort(key=lambda p: p.market_value, reverse=True)
    total_cost = sum(p.cost for p in positions)
    total_value = sum(p.market_value for p in positions)
    annual = sum(p.annual_dividend for p in positions)
    return PortfolioSummary(
        positions=positions,
        total_cost=total_cost,
        total_value=total_value,
        total_profit=total_value - total_cost,
        blended_yield_pct=(annual / total_value * 100) if total_value else 0.0,
        annual_dividend_income=annual,
        mixed_currency=len(currencies) > 1,
    )
