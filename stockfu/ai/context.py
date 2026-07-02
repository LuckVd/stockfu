"""取数适配器:把已落库的情绪指数/估值填进 AdvisorContext。

读 IndexSnapshot 最新快照(快、不调网络),不实时重算 compute_stock
(那会触发 baostock/资金流网络调用,8s 超时,不适合 AI 分析路径)。
若某层快照缺失,对应字段为 None —— 顾问会如实说"无该维度信号"。
"""
from __future__ import annotations

import json

from sqlmodel import select

from stockfu.ai.skills.advisors.base import AdvisorContext
from stockfu.db import session_scope
from stockfu.models import Asset, Holding, IndexSnapshot


def _latest_indices(session, level: str, scope: str) -> dict:
    """读某层(level/scope)最新的 fear/greed/heat + components。返回 {fear,greed,heat,_components}。"""
    rows = session.exec(
        select(IndexSnapshot)
        .where(IndexSnapshot.level == level, IndexSnapshot.scope == scope)
        .order_by(IndexSnapshot.snap_date.desc())
    ).all()
    out: dict = {}
    comps: dict = {}
    for r in rows:
        if r.index_key in ("fear", "greed", "heat") and r.index_key not in out:
            out[r.index_key] = r.value
        if r.components and not comps:
            try:
                comps = json.loads(r.components)
            except (json.JSONDecodeError, TypeError):
                pass
    out["_components"] = comps
    return out


def build_context(code: str) -> AdvisorContext:
    """从库构建顾问数据包。字段缺失即 None(顾问据此说"无信号")。"""
    with session_scope() as s:
        asset = s.get(Asset, code)
        name = asset.name if asset else ""
        sector = asset.sector if asset else ""

        stock = _latest_indices(s, "stock", code)
        market = _latest_indices(s, "market", "MARKET")
        sector_idx = _latest_indices(s, "sector", sector) if sector else {}

        holding = s.get(Holding, code)
        has_position = holding is not None and holding.shares > 0

    sc = stock.get("_components", {})
    return AdvisorContext(
        code=code,
        name=name,
        fear=stock.get("fear"),
        greed=stock.get("greed"),
        heat=stock.get("heat"),
        market_fear=market.get("fear"),
        market_greed=market.get("greed"),
        sector_fear=sector_idx.get("fear"),
        sector_greed=sector_idx.get("greed"),
        pe_pct=sc.get("pe_pct"),
        pb_pct=sc.get("pb_pct"),
        volatility_pct=sc.get("volatility_pct"),
        has_position=has_position,
    )
