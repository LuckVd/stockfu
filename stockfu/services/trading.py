"""交易录入与持仓重算。

买入/卖出写入 transaction 表，recompute_holding 按移动加权平均重算 holding。
支持多次买卖，成本自动按移动加权平均结转。
"""
from __future__ import annotations

from datetime import date

from sqlmodel import select

from stockfu.data.base import (classify_asset_type, currency_of, detect_market)
from stockfu.db import session_scope
from stockfu.models import Asset, Holding, Side, Transaction


def _ensure_asset(code: str) -> None:
    with session_scope() as s:
        if s.get(Asset, code):
            return
        m = detect_market(code)
        s.add(Asset(code=code, name="", market=m,
                    asset_type=classify_asset_type(code, m),
                    currency=currency_of(m), is_watch=True))
        s.commit()


def add_transaction(code: str, side: str, shares: float, price: float,
                    trade_date: date | None = None, note: str = "") -> dict:
    _ensure_asset(code)
    trade_date = trade_date or date.today()
    amount = round(shares * price, 2)
    with session_scope() as s:
        s.add(Transaction(asset_code=code, side=side, shares=shares,
                          price=price, amount=amount, trade_date=trade_date, note=note))
        s.commit()
    return recompute_holding(code)


def recompute_holding(code: str) -> dict:
    """按交易记录(移动加权平均)重算 holding。"""
    with session_scope() as s:
        txns = s.exec(select(Transaction).where(
            Transaction.asset_code == code
        ).order_by(Transaction.trade_date, Transaction.id)).all()
        shares = 0.0
        total_cost = 0.0
        first = None
        for t in txns:
            if t.side == Side.BUY.value:
                if first is None:
                    first = t.trade_date
                total_cost += t.amount + t.fee
                shares += t.shares
            elif t.side == Side.SELL.value and shares > 0:
                avg = total_cost / shares
                total_cost -= avg * t.shares
                shares -= t.shares
                if shares < 1e-9:
                    shares, total_cost = 0.0, 0.0
        avg_cost = (total_cost / shares) if shares > 0 else 0.0
        h = s.get(Holding, code)
        if shares > 0:
            if h is None:
                s.add(Holding(asset_code=code, shares=shares, avg_cost=avg_cost,
                              total_cost=round(total_cost, 2), first_buy_date=first))
            else:
                h.shares, h.avg_cost, h.total_cost = shares, avg_cost, round(total_cost, 2)
                h.first_buy_date = h.first_buy_date or first
        elif h is not None:
            s.delete(h)
        s.commit()
    return {"shares": shares, "avg_cost": round(avg_cost, 4),
            "total_cost": round(total_cost, 2)}


def reset_all() -> None:
    """清空全部交易与持仓（保留 asset 自选）。"""
    with session_scope() as s:
        for t in s.exec(select(Transaction)).all():
            s.delete(t)
        for h in s.exec(select(Holding)).all():
            s.delete(h)
        s.commit()


def list_holdings() -> list[dict]:
    with session_scope() as s:
        return [{"code": h.asset_code, "shares": h.shares,
                 "avg_cost": h.avg_cost, "total_cost": h.total_cost,
                 "first_buy": h.first_buy_date}
                for h in s.exec(select(Holding)).all()]
