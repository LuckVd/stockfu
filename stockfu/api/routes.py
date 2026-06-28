"""FastAPI 路由：组合/行情/分红/网格/自选/指数/资金流/板块情绪。

以 GET 只读为主，含交易/设置写端点（POST/PUT）。序列化用 jsonable_encoder，兼容 dataclass/date。
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


@router.get("/share")
def share_card():
    """分享卡片：大盘指数 + 持仓公开数据（脱敏，不含持仓数/成本/盈亏/市值/年红利）。"""
    from stockfu.services import share
    return jsonable_encoder(share.build_card())


@router.get("/quote/{code}")
def quote(code: str):
    """最新天级收盘快照（今日若已收盘落盘则当日，否则前一交易日；缺则按需补一次）。"""
    from stockfu.services.snapshot import latest_snapshot
    snap = latest_snapshot(code)
    return jsonable_encoder(snap) if snap else {"error": "no quote", "code": code}


@router.get("/dividend/{code}")
def dividend_api(code: str):
    from stockfu.services.snapshot import latest_snapshot
    mgr = get_manager()
    snap = latest_snapshot(code)
    m = mgr.get_dividend_metric(code, latest_price=snap.close if snap else None)
    return jsonable_encoder(m) if m else {"error": "no dividend", "code": code}


@router.get("/grid/{code}")
def grid_api(code: str):
    return grid.build_grid(code) or {"error": "no data", "code": code}


@router.get("/watchlist")
def watchlist():
    """自选/追踪股：现价/涨跌/股息率/三层情绪（不含持仓字段）。"""
    return jsonable_encoder(portfolio.get_watchlist_view())


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
    """市场层 fear/greed/heat。优先读今日落库（避免每次刷新都重算外部因子），
    当天首次实时算并落库，之后刷新毫秒级返回。"""
    from datetime import date as _d
    from sqlmodel import select
    from stockfu.db import session_scope
    from stockfu.models import IndexSnapshot
    from stockfu.services import composite, factors as F
    today = _d.today()
    with session_scope() as s:
        rows = s.exec(select(IndexSnapshot).where(
            IndexSnapshot.level == "market", IndexSnapshot.scope == "MARKET",
            IndexSnapshot.snap_date == today)).all()
    if rows:
        out = {"level": "market", "scope": "MARKET",
               **{r.index_key: r.value for r in rows}}
    else:
        r = composite.compute_market()   # 当天首次：实时算（慢）+ 落库
        composite.save(r)
        out = dict(r)
    closes = F.quote_series(composite.BENCH, "close", 30)
    out["today_chg"] = round((closes[-1] / closes[-2] - 1) * 100, 2) if len(closes) >= 2 else None
    # 附加副指数：创业板 / 科创50（板块层，已落库）
    out["sectors"] = {}
    with session_scope() as s:
        for name in ("创业板", "科创50"):
            srows = s.exec(select(IndexSnapshot).where(
                IndexSnapshot.level == "sector", IndexSnapshot.scope == name,
                IndexSnapshot.snap_date == today)).all()
            out["sectors"][name] = {r.index_key: r.value for r in srows}
    return out


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


@router.get("/indices/quotes")
def index_quotes():
    """主要指数当日点数/涨跌幅 + 恐/贪/热。"""
    from stockfu.services.snapshot import index_quotes_view, latest_trade_date
    return {"trade_date": latest_trade_date(), **index_quotes_view()}

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


@router.delete("/holding/{code}")
def delete_holding_api(code: str):
    """删除单只持仓（连交易流水，保留自选）。"""
    return trading.delete_holding(code)


@router.delete("/holdings")
def clear_holdings_api():
    """清空全部持仓 + 交易（保留自选）。"""
    trading.reset_all()
    return {"ok": True}


@router.post("/stock/{code}/ensure")
def ensure_stock_data(code: str, background: bool = True):
    """补该股历史K线 + 算情绪指数落库（买入/加自选后触发）。

    background=True（默认）起线程后台跑，立即返回，不阻塞前端；
    =False 同步跑完返回结果（调试用）。
    """
    import threading
    from stockfu.scheduler.jobs import ensure_stock_data_and_index

    def _run():
        try:
            ensure_stock_data_and_index(code)
        except Exception as exc:  # noqa: BLE001
            import logging
            logging.getLogger("stockfu").warning(
                "ensure_stock_data(%s) 失败: %s", code, exc)

    if background:
        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "code": code, "status": "started",
                "detail": "后台补K线+算指数中，约几十秒，完成后自动刷新"}
    return {"ok": True, "code": code, "result": ensure_stock_data_and_index(code)}


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


@router.post("/watch/{code}")
def add_watch(code: str):
    """加追踪/自选（不产生持仓）。前端随后调 /stock/{code}/ensure 后台补K线+情绪。"""
    return trading.add_watch(code)


@router.delete("/watch/{code}")
def remove_watch(code: str):
    """取消追踪/自选（保留 asset 与历史数据；已持仓不受影响）。"""
    return trading.remove_watch(code)


# ---------- 设置：外网代理（yfinance 抓港美股用）----------

@router.get("/config/proxy")
def get_proxy_config():
    """当前外网代理。source: db=面板设置过；env=未设，用 .env 默认。"""
    from stockfu.config import get_overseas_proxy
    from stockfu.db import get_app_config, has_app_config
    has = has_app_config("overseas_proxy")
    return {
        "proxy_url": get_app_config("overseas_proxy") if has else "",
        "source": "db" if has else "env",
        "effective": get_overseas_proxy(),
    }


@router.put("/config/proxy")
def set_proxy_config(payload: dict = Body(...)):
    """保存外网代理。payload: {proxy_url}；空串=直连。"""
    from stockfu.config import set_overseas_proxy
    raw = payload.get("proxy_url")
    if raw is not None and not isinstance(raw, str):
        return {"error": "proxy_url 必须是字符串"}
    return {"ok": True, "effective": set_overseas_proxy(raw if raw is not None else "")}


@router.post("/config/proxy/test")
def test_proxy_config(payload: dict | None = Body(default=None)):
    """测试代理连通性。payload: {proxy_url?}；不传则测当前生效代理。三态判定。"""
    from stockfu.config import test_overseas_proxy
    payload = payload or {}
    raw = payload.get("proxy_url")
    url = raw.strip() if isinstance(raw, str) else None
    return test_overseas_proxy(url)


@router.get("/config/schedule")
def get_schedule_config():
    """定时抓取配置：daily_fetch_time / retry_interval / retry_count（北京时间）。"""
    from stockfu.config import (get_daily_fetch_time, get_fetch_retry_count,
                                get_fetch_retry_interval)
    from stockfu.db import has_app_config
    return {
        "daily_fetch_time": get_daily_fetch_time(),
        "fetch_retry_interval": get_fetch_retry_interval(),
        "fetch_retry_count": get_fetch_retry_count(),
        "source": "db" if has_app_config("daily_fetch_time") else "default",
    }


@router.put("/config/schedule")
def set_schedule_config(payload: dict = Body(...)):
    """保存定时抓取配置（任一项可选，未传不动）。"""
    from stockfu.config import (set_daily_fetch_time, set_fetch_retry_count,
                                set_fetch_retry_interval)
    out: dict = {}
    if "daily_fetch_time" in payload:
        out["daily_fetch_time"] = set_daily_fetch_time(str(payload["daily_fetch_time"]))
    if "fetch_retry_interval" in payload:
        out["fetch_retry_interval"] = set_fetch_retry_interval(payload["fetch_retry_interval"])
    if "fetch_retry_count" in payload:
        out["fetch_retry_count"] = set_fetch_retry_count(payload["fetch_retry_count"])
    return {"ok": True, **out}
