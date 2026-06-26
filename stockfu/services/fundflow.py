"""大资金流向：宽基/行业 ETF 的成交额活跃度 + 份额变化(若有)。

数据源现实：akshare 免费接口拿不到 ETF 实时份额(shares 全 None)，故主指标用
「成交额活跃度」(近5日均额 / 历史均额 × 50)作为资金活跃度代理；份额序列若有则附带。
bias 判断资金在宽基(避险) vs 行业(进攻) 间的偏好。
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlmodel import select

from stockfu.data.manager import get_manager
from stockfu.db import session_scope
from stockfu.models import FundFlowSnapshot, QuoteSnapshot
from stockfu.scheduler.jobs import INDEX_ETFS

BROAD = {"510300", "510500", "159915", "512100", "588000"}
INDUSTRY = {"512480", "512690", "512010", "515030", "512800"}


def _amount_series(code: str, lookback: int = 30) -> list[float]:
    start = date.today() - timedelta(days=lookback + 5)
    with session_scope() as s:
        rows = s.exec(select(QuoteSnapshot).where(
            QuoteSnapshot.asset_code == code,
            QuoteSnapshot.quote_date >= start,
        ).order_by(QuoteSnapshot.quote_date)).all()
    return [r.amount for r in rows if r.amount]


def _shares_series(code: str, lookback: int = 30) -> list[float]:
    start = date.today() - timedelta(days=lookback + 5)
    with session_scope() as s:
        rows = s.exec(select(FundFlowSnapshot).where(
            FundFlowSnapshot.etf_code == code,
            FundFlowSnapshot.snap_date >= start,
        ).order_by(FundFlowSnapshot.snap_date)).all()
    return [r.shares_outstanding for r in rows if r.shares_outstanding]


def etf_flow(code: str, lookback: int = 30) -> dict:
    amts = _amount_series(code, lookback)
    shs = _shares_series(code, lookback)
    rt = get_manager().get_etf_fund_flow(code)

    heat = None
    if len(amts) >= 10:
        a5 = sum(amts[-5:]) / 5
        base = sum(amts[:-5]) / max(1, len(amts) - 5)
        heat = round(a5 / base * 50, 2) if base > 0 else None  # 50=平量

    delta = round(shs[-1] - shs[0], 4) if len(shs) >= 2 else None
    return {
        "code": code,
        "category": "broad" if code in BROAD else ("industry" if code in INDUSTRY else "other"),
        "amount_points": len(amts),
        "amount_heat": heat,                       # 成交额活跃度（主指标）
        "latest_amount_yi": round(amts[-1] / 1e8, 2) if amts else None,
        "shares_points": len(shs),
        "shares_delta": delta,                      # 份额净变化（多数情况为 None：免费源不提供）
        "latest_nav": rt.get("nav"),
    }


def flow_board(lookback: int = 30) -> dict:
    """全部追踪 ETF 的资金流看板 + 宽基/行业偏好。"""
    etfs = [etf_flow(c, lookback) for c in INDEX_ETFS]
    broad_h = [e["amount_heat"] for e in etfs if e["code"] in BROAD and e["amount_heat"]]
    ind_h = [e["amount_heat"] for e in etfs if e["code"] in INDUSTRY and e["amount_heat"]]
    bh = sum(broad_h) / len(broad_h) if broad_h else 0
    ih = sum(ind_h) / len(ind_h) if ind_h else 0
    bias = ("进攻偏好(行业更活跃)" if ih > bh + 5
            else "避险偏好(宽基更活跃)" if bh > ih + 5
            else "均衡")
    return {
        "etfs": etfs,
        "broad_heat": round(bh, 1), "industry_heat": round(ih, 1),
        "bias": bias, "lookback_days": lookback,
    }
