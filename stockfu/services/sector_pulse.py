"""分享图的行业全景：只汇总同日、同分类的原始行情与主力资金流。"""
from __future__ import annotations

from datetime import date

from sqlmodel import select

from stockfu.db import session_scope
from stockfu.models import SectorFlowSnapshot, SectorSnapshot
from stockfu.services import composite as C
from stockfu.services import factors as F


def _pct(values: list[float], value: float | None) -> float | None:
    return F.percentile(values, value)[0] if value is not None else None


def _vol(closes: list[float], n: int = 20) -> list[float]:
    return C._rolling_vol(closes, n)


def _change(closes: list[float], n: int = 5) -> list[float]:
    return C._rolling_chg(closes, n)


def _activity(amounts: list[float], n: int = 20) -> list[float]:
    return C._rolling_relative_activity(amounts, n)


def _mean(values: list[float | None]) -> float | None:
    good = [x for x in values if x is not None]
    return round(sum(good) / len(good), 2) if good else None


def _state(flows: list[float]) -> str:
    recent = flows[-5:]
    if len(recent) < 3:
        if not recent:
            return "资金样本不足"
        return "当日净流入" if recent[-1] > 0 else "当日净流出" if recent[-1] < 0 else "当日平衡"
    if all(x > 0 for x in recent):
        return "连续流入"
    if all(x < 0 for x in recent):
        return "连续流出"
    if recent[-1] > 0 and sum(recent[:-1]) <= 0:
        return "转强"
    if recent[-1] < 0 and sum(recent[:-1]) >= 0:
        return "转弱"
    return "资金分歧"


def build(as_of: date) -> dict:
    """构建全行业分享数据；只返回行情和资金流均为 as_of 的行业。"""
    with session_scope() as s:
        quote_rows = s.exec(select(SectorSnapshot).where(
            SectorSnapshot.snap_date <= as_of).order_by(
            SectorSnapshot.sector_name, SectorSnapshot.snap_date)).all()
        flow_rows = s.exec(select(SectorFlowSnapshot).where(
            SectorFlowSnapshot.snap_date <= as_of).order_by(
            SectorFlowSnapshot.sector_name, SectorFlowSnapshot.snap_date)).all()
    quotes: dict[str, list] = {}
    flows: dict[str, list] = {}
    for r in quote_rows:
        quotes.setdefault(r.sector_name, []).append(r)
    for r in flow_rows:
        flows.setdefault(r.sector_name, []).append(r)
    rows = []
    for name, qs in quotes.items():
        fs = flows.get(name, [])
        if not qs or not fs or qs[-1].snap_date != as_of or fs[-1].snap_date != as_of:
            continue
        closes = [x.close for x in qs if x.close and x.close > 0]
        amounts = [x.amount for x in qs if x.amount is not None and x.amount >= 0]
        net = [x.net_inflow for x in fs if x.net_inflow is not None]
        if len(closes) < 26:
            continue
        price_pcts = C.price_factor_percentiles(closes, amounts)
        chgs = _change(closes)
        rows.append({
            "name": name, "day_chg": qs[-1].pct_chg,
            "perf_5d": round(chgs[-1] * 100, 2) if chgs else None,
            "net_inflow": fs[-1].net_inflow,
            "net_inflow_pct": fs[-1].net_inflow_pct,
            "leading_stock": fs[-1].leading_stock or None,   # 领涨股名（空串/None 统一为 None）
            "leading_chg": fs[-1].leading_chg,               # 领涨股涨跌幅%
            "_vol": price_pcts["volatility_pct"],
            "_momentum": price_pcts["momentum_pct"],
            "_activity": price_pcts["relative_activity_pct"],
            "_net": fs[-1].net_inflow, "state": _state(net),
        })
    # 资金流是当日行业轮动信号：用同日横截面净流入排名表达资金聚集程度。
    # 它只进入热度，不改变基于自身历史价格分位的恐慌/贪婪。
    net_values = [r["_net"] for r in rows if r["_net"] is not None]
    for r in rows:
        flow_heat = (_pct(net_values, r["_net"])
                     if r["_net"] is not None and len(net_values) >= 10 else None)
        vol, momentum, activity = r.pop("_vol"), r.pop("_momentum"), r.pop("_activity")
        r.pop("_net")
        r["fear"] = _mean([vol, 100 - momentum if momentum is not None else None])
        r["greed"] = _mean([momentum])
        r["heat"] = _mean([activity, flow_heat])
        r["flow_heat"] = flow_heat
    rows.sort(key=lambda x: (x["net_inflow"] is not None, x["net_inflow"] or 0), reverse=True)
    up = sum(1 for x in rows if (x["day_chg"] or 0) > 0)
    down = sum(1 for x in rows if (x["day_chg"] or 0) < 0)
    return {"date": as_of.isoformat(), "count": len(rows), "up": up, "down": down,
            "net_inflow": round(sum(x["net_inflow"] or 0 for x in rows), 2), "rows": rows}
