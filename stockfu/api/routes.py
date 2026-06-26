"""FastAPI 路由：组合/行情/分红/网格/自选/指数/资金流/板块情绪。

所有端点只读（GET），返回 JSON。序列化用 jsonable_encoder，兼容 dataclass/date。
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Body, Query
from fastapi.encoders import jsonable_encoder
from sqlmodel import select

from stockfu.data.manager import get_manager
from stockfu.db import session_scope
from stockfu.models import Asset
from stockfu.services import (dividend, fundflow, grid, indices, portfolio,
                           sentiment, trading)

router = APIRouter()


@router.get("/health")
def health():
    return {"ok": True, "app": "stockfu"}


@router.get("/portfolio")
def get_portfolio_api():
    """持仓总览：各标的市值/盈亏/股息率/年红利/回本 + 组合整体。"""
    return jsonable_encoder(portfolio.get_portfolio())


@router.get("/quote/{code}")
def quote(code: str):
    q = get_manager().get_quote(code)
    return jsonable_encoder(q) if q else {"error": "no quote", "code": code}


@router.get("/dividend/{code}")
def dividend_api(code: str):
    mgr = get_manager()
    q = mgr.get_quote(code)
    m = mgr.get_dividend_metric(code, latest_price=q.price if q else None)
    return jsonable_encoder(m) if m else {"error": "no dividend", "code": code}


@router.get("/grid/{code}")
def grid_api(code: str):
    return grid.build_grid(code) or {"error": "no data", "code": code}


@router.get("/watchlist")
def watchlist():
    with session_scope() as s:
        rows = s.exec(select(Asset).where(Asset.is_watch == True)).all()  # noqa: E712
        return [{"code": a.code, "name": a.name, "market": a.market,
                 "type": a.asset_type, "currency": a.currency} for a in rows]


# ---------- P1：市场情绪 / 资金流 ----------

@router.get("/indices")
def indices_api():
    """自定义指数：恐慌/热度 当日值 + 近30日历史。"""
    cur: dict[str, float] = {}
    for k, fn in (("fear", indices.compute_fear), ("heat", indices.compute_heat)):
        v = fn()
        if v is not None:
            cur[k] = v
    return {"current": cur, "history": indices.latest(30)}


@router.get("/indices/market")
def indices_market():
    """市场层 fear/greed/heat（多因子分位合成）。"""
    from stockfu.services import composite
    return composite.compute_market()


@router.get("/indices/sector/{name}")
def indices_sector(name: str):
    """板块层 fear/greed/heat。name 见 composite.SECTOR_MAP。"""
    from stockfu.services import composite
    etf = composite.SECTOR_MAP.get(name)
    if not etf:
        return {"error": "unknown sector", "available": list(composite.SECTOR_MAP)}
    return composite.compute_sector(etf, name)


@router.get("/indices/stock/{code}")
def indices_stock(code: str):
    """个股层 fear/greed/heat。"""
    from stockfu.services import composite
    return composite.compute_stock(code)


@router.get("/indices/history")
def indices_history(level: str = "market", scope: str = "MARKET", days: int = 30):
    """某层某 scope 的指数历史序列。"""
    from datetime import date, timedelta
    from stockfu.models import IndexSnapshot
    start = date.today() - timedelta(days=days + 5)
    with session_scope() as s:
        rows = s.exec(select(IndexSnapshot).where(
            IndexSnapshot.level == level, IndexSnapshot.scope == scope,
            IndexSnapshot.snap_date >= start).order_by(IndexSnapshot.snap_date)).all()
    out: dict[str, list] = {}
    for r in rows:
        out.setdefault(r.index_key, []).append({"date": r.snap_date.isoformat(), "value": r.value})
    return out


@router.get("/fundflow")
def fundflow_api(lookback: int = Query(30, ge=1, le=180)):
    """大资金流向：宽基/行业 ETF 份额变化 + 偏好判断。"""
    return fundflow.flow_board(lookback)


@router.get("/sentiment")
def sentiment_api(top_n: int = Query(10, ge=1, le=50)):
    """板块情绪：行业资金流排名 + 温度。"""
    return sentiment.sector_board(top_n)


@router.get("/sectors")
def sectors(top_n: int = Query(8, ge=1, le=30)):
    """板块资金流原始排名（兼容旧端点）。"""
    return get_manager().get_sector_fund_flow(top_n)


@router.get("/fundflow/{code}")
def fundflow_one(code: str):
    return get_manager().get_stock_fund_flow(code) or {"error": "no data", "code": code}


# ---------- 交易录入（前端买卖，对应 TUI 的 b/s）----------

@router.get("/holdings")
def holdings_api():
    """当前持仓列表。"""
    return trading.list_holdings()


@router.post("/trade")
def trade(payload: dict = Body(...)):
    """买入/卖出。payload: {code, side:'buy'|'sell', shares, price, date?}"""
    code = str(payload.get("code", "")).strip()
    side = str(payload.get("side", "")).strip()
    if side not in ("buy", "sell"):
        return {"error": "side 必须是 buy 或 sell"}
    try:
        shares = float(payload["shares"])
        price = float(payload["price"])
    except (KeyError, ValueError, TypeError):
        return {"error": "shares / price 必须是数字"}
    d = payload.get("date")
    td = None
    if d:
        try:
            td = date.fromisoformat(str(d))
        except ValueError:
            return {"error": "date 格式应为 YYYY-MM-DD"}
    return trading.add_transaction(code, side, shares, price, td)
