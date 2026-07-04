"""取数适配器:把已落库的情绪指数/估值填进 AdvisorContext。

读 IndexSnapshot 最新快照(快、不调网络),不实时重算 compute_stock
(那会触发 baostock/资金流网络调用,8s 超时,不适合 AI 分析路径)。
若某层快照缺失,对应字段为 None —— 顾问会如实说"无该维度信号"。

例外:PE/PB 分位缺时,现调 baostock 单查补算(~1-2s,远轻于 compute_stock)
并回填 IndexSnapshot,下次免算 —— 根治 compute 时 baostock 偶发失败留空。
"""
from __future__ import annotations

import json

from sqlmodel import select

from datetime import date, timedelta

from stockfu.ai.skills.advisors.base import AdvisorContext
from stockfu.db import session_scope
from stockfu.models import Asset, DividendEvent, Holding, IndexSnapshot, QuoteSnapshot
from stockfu.services.factors import ma_alignment

# 板块 fallback(asset.sector 空时兜底,覆盖常分析标的 → 板块名)。
# 当个股被映射到已有 sector 层 IndexSnapshot 的板块时,context 能把 sector_fear/greed 带上。
CODE_SECTOR_FALLBACK = {
    "002594": "新能源车", "000625": "新能源车",
    "600519": "白酒", "000858": "白酒",
    "600036": "银行", "601318": "保险",
    "512480": "半导体", "512690": "白酒",
    "510300": "沪深300", "510500": "中证500", "159915": "创业板", "588000": "科创50",
    "512800": "银行", "512010": "医药", "515030": "新能源车",
}


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


def _fetch_pe_pb(code: str) -> tuple[float | None, float | None]:
    """baostock 现算 PE/PB 分位(3 次重试 + 掉线重连,复用 composite 的稳健逻辑)。
    仅 ~1-2s,远轻于 compute_stock 的全量网络调用。"""
    try:
        from stockfu.data.baostock_source import BaostockSource
        from stockfu.data.manager import get_manager
        bs = get_manager().baostock
        pe_pb = None
        for _ in range(3):
            pe_pb = bs.get_pe_pb_percentile(code)
            if pe_pb and (pe_pb[0] is not None or pe_pb[1] is not None):
                return pe_pb
            BaostockSource.force_relogin()
        return pe_pb or (None, None)
    except Exception:  # noqa: BLE001
        return None, None


def _backfill_components(session, code: str, pe_pct: float | None, pb_pct: float | None) -> None:
    """回填 pe_pct/pb_pct 到最新 stock IndexSnapshot 的 components,下次免现算。"""
    if pe_pct is None and pb_pct is None:
        return
    try:
        row = session.exec(
            select(IndexSnapshot)
            .where(
                IndexSnapshot.level == "stock",
                IndexSnapshot.scope == code,
                IndexSnapshot.components.isnot(None),
            )
            .order_by(IndexSnapshot.snap_date.desc())
        ).first()
        if row is None:
            return
        existing = json.loads(row.components) if row.components else {}
        if pe_pct is not None:
            existing["pe_pct"] = pe_pct
        if pb_pct is not None:
            existing["pb_pct"] = pb_pct
        row.components = json.dumps(existing, ensure_ascii=False)
        session.add(row)
        session.commit()
    except Exception:  # noqa: BLE001  回填失败不影响本次(已现算填进 context)
        pass


def build_context(code: str) -> AdvisorContext:
    """从库构建顾问数据包。字段缺失即 None(顾问据此说"无信号")。"""
    with session_scope() as s:
        asset = s.get(Asset, code)
        name = asset.name if asset else ""
        sector = (asset.sector or "").strip() or CODE_SECTOR_FALLBACK.get(code, "")

        stock = _latest_indices(s, "stock", code)
        market = _latest_indices(s, "market", "MARKET")
        sector_idx = _latest_indices(s, "sector", sector) if sector else {}

        holding = s.get(Holding, code)
        has_position = holding is not None and holding.shares > 0

        # today_chg + 最新收盘价
        q = s.exec(select(QuoteSnapshot).where(
            QuoteSnapshot.asset_code == code,
        ).order_by(QuoteSnapshot.quote_date.desc())).first()
        close = q.close if q else None
        today_chg = q.pct_chg if q else None

        # profit_pct(持仓盈亏 %)
        profit_pct = None
        if has_position and close and holding.avg_cost:
            profit_pct = round((close - holding.avg_cost) / holding.avg_cost * 100, 2)

        # dividend_yield(TTM 股息率 %)
        one_year_ago = date.today() - timedelta(days=365)
        divs = s.exec(select(DividendEvent).where(
            DividendEvent.asset_code == code,
            DividendEvent.ex_date >= one_year_ago,
        )).all()
        ttm_cash = sum(d.per_share_cash for d in divs)  # per_share_cash 已归一化为"每股"(数据源取时已÷10)
        dividend_yield = round(ttm_cash / close * 100, 2) if close and ttm_cash else None

    sc = stock.get("_components", {})

    # PE/PB 分位缺 → baostock 现算补 + 回填(下次免算)
    if sc.get("pe_pct") is None or sc.get("pb_pct") is None:
        pe_pct, pb_pct = _fetch_pe_pb(code)
        if pe_pct is not None:
            sc["pe_pct"] = pe_pct
        if pb_pct is not None:
            sc["pb_pct"] = pb_pct
        _backfill_components(s, code, pe_pct, pb_pct)

    # ma_alignment(均线排列,纯本地,快)
    ma_align = ma_alignment(code) if code else None

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
        today_chg=today_chg,
        profit_pct=profit_pct,
        dividend_yield=dividend_yield,
        ma_alignment=ma_align,
        has_position=has_position,
    )
