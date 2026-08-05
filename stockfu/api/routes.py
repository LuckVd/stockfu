"""FastAPI 路由：组合/行情/分红/自选/指数/资金流/板块情绪。

以 GET 只读为主，含交易/设置写端点（POST/PUT）。序列化用 jsonable_encoder，兼容 dataclass/date。
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Body, HTTPException, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, Response
from sqlmodel import select

from stockfu.data.manager import get_manager
from stockfu.db import session_scope
from stockfu.models import Asset
from stockfu.services import portfolio, trading

router = APIRouter()


@router.get("/health")
def health():
    return {"ok": True, "app": "stockfu"}


@router.get("/portfolio")
def get_portfolio_api():
    """持仓总览：各标的市值/盈亏/股息率/年红利/回本 + 组合整体。"""
    return jsonable_encoder(portfolio.get_portfolio())


@router.get("/share")
def share_card(request: Request):
    """分享卡片：大盘指数 + 持仓公开数据（脱敏，不含持仓数/成本/盈亏/市值/年红利）。

    邮件渲染（render_share_images 注入 X-Mail-Render 头）放宽到只校验指数——
    邮件已不渲染个股持仓页；web 浏览器手动导出保持严格（自选股须齐全）。
    """
    from stockfu.services import share
    include_watch = request.headers.get("x-mail-render") != "1"
    try:
        return jsonable_encoder(share.build_card(include_watch=include_watch))
    except ValueError as exc:
        # 数据日期不一致时明确拒绝，避免浏览器下载一张貌似当天、实为混合日期的图。
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/watchlist")
def watchlist():
    """自选/追踪股：现价/涨跌/股息率/三层情绪（不含持仓字段）。"""
    return jsonable_encoder(portfolio.get_watchlist_view())


# ---------- P1：市场情绪 / 资金流 ----------

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


@router.get("/sectors/flow")
def sector_flow_today_api(top_n: int = Query(10, ge=1, le=90)):
    """板块当日主力资金流即时排名（同花顺，列全且不受东财限流；按净额降序）。

    只读实时；历史落库由每日 `--fetch --date` 负责（`backfill_sector_flow`）。
    """
    rows = get_manager().get_sector_flow_today()
    rows = sorted(rows, key=lambda x: x.get("net_inflow") or 0, reverse=True)
    return {"count": len(rows),
            "top": rows[:top_n],
            "bottom": list(reversed(rows[-top_n:])) if len(rows) > top_n else []}


# ---------- 交易录入（前端买卖）----------

@router.delete("/holding/{code}")
def delete_holding_api(code: str):
    """删除单只持仓（连交易流水，保留自选）。"""
    return trading.delete_holding(code)


@router.delete("/holdings")
def clear_holdings_api():
    """清空全部持仓 + 交易（保留自选）。"""
    trading.reset_all()
    return {"ok": True}


def _trigger_bg_ensure(code: str, target_date=None) -> None:
    """后台补该股历史K线 + 算情绪指数（买入/加自选/CSV 导入新代码后触发）。"""
    import logging
    import threading

    from stockfu.scheduler.jobs import ensure_stock_data_and_index

    def _run():
        try:
            ensure_stock_data_and_index(code, target_date=target_date)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("stockfu").warning(
                "ensure_stock_data(%s) 失败: %s", code, exc)

    threading.Thread(target=_run, daemon=True).start()


@router.post("/stock/{code}/ensure")
def ensure_stock_data(code: str, background: bool = True, date: str | None = None):
    """补该股历史K线 + 算情绪指数落库（买入/加自选后触发）。

    background=True（默认）起线程后台跑，立即返回，不阻塞前端；
    =False 同步跑完返回结果（调试用）。
    date: 可选目标交易日(YYYY-MM-DD)；缺省取已收盘的最近交易日(过校验)。
    """
    from stockfu.scheduler.jobs import ensure_stock_data_and_index

    if background:
        _trigger_bg_ensure(code, target_date=date)
        return {"ok": True, "code": code, "status": "started",
                "detail": "后台补K线+算指数中，约几十秒，完成后自动刷新"}
    return {"ok": True, "code": code,
            "result": ensure_stock_data_and_index(code, target_date=date)}


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


@router.get("/config/mail")
def get_mail_config_api():
    """邮件配置（密码脱敏为 has_password）。"""
    from stockfu.config import get_mail_config
    return get_mail_config()


@router.put("/config/mail")
def set_mail_config_api(payload: dict = Body(...)):
    """保存邮件配置（任一项可选；空 smtp_pass = 不改密码）。"""
    from stockfu.config import get_mail_config, set_mail_config
    set_mail_config(payload or {})
    return get_mail_config()


@router.post("/config/mail/test")
def test_mail_api():
    """立即生成多图并发一封测试邮件（需 --serve 在跑 + SMTP 已配置）。"""
    from stockfu.services.mail import run_mail_job
    return run_mail_job()


# ---------- 策略信号扫描 / 逐股订阅 ----------

def _ensure_signal_schema() -> None:
    from stockfu.services.signal_scan import ensure_signal_schema
    ensure_signal_schema()


@router.get("/signals/config")
def get_signal_config_api():
    _ensure_signal_schema()
    from stockfu.services.signal_scan import signal_config_view
    return jsonable_encoder(signal_config_view())


@router.put("/signals/config")
def set_signal_config_api(payload: dict = Body(...)):
    _ensure_signal_schema()
    from stockfu.services.signal_scan import update_signal_config
    try:
        return jsonable_encoder(update_signal_config(payload or {}))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/signals/subscriptions")
def get_signal_subscriptions_api(as_of: str | None = None):
    _ensure_signal_schema()
    from stockfu.services.recommend import default_as_of
    from stockfu.services.signal_scan import subscription_rows
    try:
        signal_date = date.fromisoformat(as_of[:10]) if as_of else default_as_of()
        return {"as_of": signal_date.isoformat(), "rows": jsonable_encoder(subscription_rows(signal_date))}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.put("/signals/subscriptions")
def set_signal_subscriptions_api(payload: dict = Body(...)):
    _ensure_signal_schema()
    from stockfu.services.signal_scan import set_subscriptions
    updates = payload.get("updates") if isinstance(payload, dict) else None
    if not isinstance(updates, list):
        raise HTTPException(status_code=400, detail="updates 必须是数组")
    return set_subscriptions(updates)


@router.get("/signals/latest")
def get_latest_signals_api(all_results: bool = False):
    _ensure_signal_schema()
    from stockfu.services.signal_scan import signal_report
    return jsonable_encoder(signal_report(subscribed_only=not all_results))


@router.post("/signals/run")
def run_signal_scan_api(payload: dict | None = Body(default=None)):
    """手工运行一次扫描；批量网络刷新由 scheduler/CLI 流水线负责。"""
    _ensure_signal_schema()
    from stockfu.services.recommend import default_as_of
    from stockfu.services.signal_scan import run_signal_scan
    body = payload or {}
    try:
        signal_date = date.fromisoformat(str(body["signal_date"])[:10]) if body.get("signal_date") else default_as_of()
        return jsonable_encoder(run_signal_scan(signal_date))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/signals/mail/test")
def test_signal_mail_api():
    _ensure_signal_schema()
    from stockfu.services.signal_mail import run_signal_mail_job
    return run_signal_mail_job(force=True)


@router.get("/signals/mail-view", response_class=HTMLResponse, include_in_schema=False)
def signal_mail_view(run_id: int | None = None):
    _ensure_signal_schema()
    from stockfu.services.signal_mail import build_signal_mail_html
    from stockfu.services.signal_scan import signal_report
    report = signal_report(run_id=run_id, subscribed_only=True)
    return HTMLResponse(build_signal_mail_html(report))


@router.get("/config/llm")
def get_llm_config_api():
    """LLM 配置（api_key 脱敏为 has_api_key）。"""
    from stockfu.config import get_llm_config
    return get_llm_config()


@router.put("/config/llm")
def set_llm_config_api(payload: dict = Body(...)):
    """保存 LLM 配置（任一项可选；空 api_key = 不改）。"""
    from stockfu.config import get_llm_config, set_llm_config
    set_llm_config(payload or {})
    return get_llm_config()


@router.post("/config/llm/test")
def test_llm_api():
    """测试 LLM 连通性（发一条极简消息探活，需已配置 base_url + api_key）。"""
    from stockfu.ai.client import LLMError, chat
    try:
        reply = chat([{"role": "user", "content": "ping"}], max_tokens=8, timeout=20, retries=0)
        return {"ok": True, "detail": "连接正常", "reply": (reply or "")[:80]}
    except LLMError as exc:
        return {"ok": False, "detail": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}


# ---------- AI 顾问分析 ----------

def _ai_key(code: str) -> str:
    return f"ai_analysis:{code}"


def _set_ai_pending(code: str) -> None:
    """标记该股票正在分析中。刷新后前端据此恢复 loading 态,避免重复点击。"""
    import json
    from datetime import datetime
    from stockfu.db import set_app_config
    set_app_config(_ai_key(code), json.dumps({
        "status": "pending",
        "pending_since": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }, ensure_ascii=False))


def _set_ai_done(code: str, result) -> None:
    """分析完成(成功带 result / 异常或全降级带 None)。写最终状态、清除 pending。
    signal 一并存,让前端 AiButton done 态零请求上色。"""
    import json
    from datetime import datetime
    from stockfu.db import set_app_config
    signal = (result or {}).get("aggregate", {}).get("final_signal") if result else None
    set_app_config(_ai_key(code), json.dumps({
        "status": "done",
        "result": result,
        "signal": signal,
        "analyzed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }, ensure_ascii=False))


from concurrent.futures import ThreadPoolExecutor as _TPE, TimeoutError as _FutTimeout
_AI_EXEC = _TPE(max_workers=2)


@router.post("/ai/{code}")
def ai_analysis(code: str):
    """运行 AI 4 顾问分析并返回含工具调用记录的结果。

    整体 180s 超时兜底:analyze 内部工具取数(baostock/akshare)偶发 socket hang,
    无超时会致 pending 永驻;超时则 _set_ai_done(None) 让前端恢复可重试。
    """
    from stockfu.ai.analyze import analyze
    _set_ai_pending(code)            # 先标记进行中(刷新后前端可恢复 loading)
    try:
        fut = _AI_EXEC.submit(lambda: jsonable_encoder(analyze(code)))
        try:
            result = fut.result(timeout=180)
        except _FutTimeout:
            _set_ai_done(code, None)
            raise HTTPException(status_code=504, detail="AI 分析超时(180s),可能是数据源无响应,请稍后重试")
    except HTTPException:
        raise
    except Exception as exc:
        _set_ai_done(code, None)     # 异常也要清 pending,避免僵尸态
        raise HTTPException(status_code=500, detail=f"AI 分析失败: {exc}")
    _set_ai_done(code, result if result.get("opinions") else None)
    return result


@router.get("/ai/result/{code}")
def get_ai_result(code: str):
    """读取该股票 AI 分析状态:done(带 result)/ pending(分析中)/ none。"""
    import json
    from stockfu.db import get_app_config, has_app_config
    if not has_app_config(_ai_key(code)):
        return {"status": "none"}
    try:
        data = json.loads(get_app_config(_ai_key(code)))
    except Exception:  # noqa: BLE001
        return {"status": "none"}
    if data.get("status") == "pending":
        return {"status": "pending", "pending_since": data.get("pending_since"), "signal": data.get("signal")}
    return {"status": "done", "result": data.get("result"), "signal": data.get("signal"),
            "analyzed_at": data.get("analyzed_at")}


# ---------- 个股 K 线（AI 报告迷你图用）----------
@router.get("/quote/kline/{code}")
def quote_kline(code: str, days: int = Query(30, ge=5, le=120)):
    """个股收盘价序列（读 quote_snapshot；AI 报告 30 日迷你图用）。"""
    from datetime import date, timedelta
    from stockfu.models import QuoteSnapshot
    start = date.today() - timedelta(days=days + 15)  # +15 缓冲跳过周末/节假日
    with session_scope() as s:
        rows = s.exec(select(QuoteSnapshot).where(
            QuoteSnapshot.asset_code == code,
            QuoteSnapshot.quote_date >= start,
        ).order_by(QuoteSnapshot.quote_date)).all()
    pts = [{"date": r.quote_date.isoformat(), "close": r.close} for r in rows[-days:]]
    return {"code": code, "days": days, "points": pts}


# ---- CSV 导入 / 导出（WebUI 工具栏：持仓 / 自选）-------------------------------
# 自选 = asset 表（追踪股票清单）；持仓 = transaction 表（holding 由其移动加权派生）。
# 导出 = 下载 CSV 文件；导入 = 上传 CSV 文件（合并 upsert，不删现有数据）。
CSV_SCOPE_FILES = {"holdings": "holdings.csv", "watchlist": "watchlist.csv"}


@router.get("/csv/template/{scope}")
def csv_template(scope: str):
    """下载 CSV 模板（表头 + 示例行）。scope: holdings | watchlist。"""
    from stockfu.services.io_csv import resolve_scope, template_text
    try:
        tables = resolve_scope(scope)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return Response(
        content=template_text(tables[0]), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{scope}-template.csv"'})


@router.get("/csv/export/{scope}")
def csv_export_scope(scope: str):
    """导出为 CSV 文件下载。持仓=交易流水，自选=追踪股票清单(asset 表)。"""
    from stockfu.services.io_csv import export_table_text, resolve_scope
    try:
        tables = resolve_scope(scope)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    text, _n = export_table_text(tables[0])
    return Response(
        content=text, media_type="text/csv",
        headers={"Content-Disposition":
                 f'attachment; filename="{CSV_SCOPE_FILES.get(scope, scope + ".csv")}"'})


@router.post("/csv/import/{scope}")
async def csv_import_scope(scope: str, file: UploadFile):
    """上传 CSV 文件合并导入（upsert，不删现有数据）。

    持仓：中文方向→枚举 → 补 amount → 导入 transaction → ensure_asset + 按移动加权重算 holding；
    自选：导入 asset，新代码后台补 K线+情绪指数。
    任何解析错都转成可读 JSON 错误（避免裸 500 让前端 JSON 解析崩）。
    """
    import csv
    import io

    from stockfu.services import trading
    from stockfu.services.io_csv import (COLUMN_CN, REQUIRED_COL, TEMPLATE_COLS,
                                         alias_headers, fill_transaction_amount,
                                         import_table_text, normalize_side_values,
                                         normalize_text, resolve_scope)
    try:
        tables = resolve_scope(scope)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    name = tables[0]
    fname = file.filename or "CSV"
    try:
        raw = await file.read()
        text = None
        for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk"):   # 容忍 Excel 的 GBK/GB18030 存档
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            text = raw.decode("utf-8", errors="replace")
        text = normalize_text(text)                       # 去 BOM、全角逗号/分号→半角
        text = alias_headers(name, text)                  # 中文表头 → 英文列名
        need = REQUIRED_COL.get(name)
        fields = csv.DictReader(io.StringIO(text)).fieldnames or []
        if need and need not in fields:                   # 表头没认出来 → 给清晰提示
            expect = ",".join(COLUMN_CN[name][c] for c in TEMPLATE_COLS[name])
            return {"ok": False,
                    "error": f"未识别到表头（找不到「{COLUMN_CN[name][need]}」列）。"
                             f"请用英文逗号分隔，首行应为：{expect}"}
        if scope == "holdings":
            text = normalize_side_values(text)           # 买入/卖出/分红 → buy/sell/dividend
            text = fill_transaction_amount(text)         # 补 amount = shares*price

        code_col = "asset_code" if scope == "holdings" else "code"
        codes_in_file = [r.get(code_col) for r in csv.DictReader(io.StringIO(text))]
        codes_in_file = [c.strip() for c in codes_in_file if c and c.strip()]

        with session_scope() as s:                       # 导入前快照，判"新代码"
            existed = {a.code for a in s.exec(select(Asset)).all()}
        counts = import_table_text(name, text)

        bg_new: list[str] = []
        if scope == "holdings":
            for c in codes_in_file:
                trading.ensure_asset(c)                  # 补 asset 行（持仓缺名时展示用）
                trading.recompute_holding(c)             # 按移动加权成本重算 holding
        else:
            for c in codes_in_file:
                if c not in existed:
                    bg_new.append(c)
                    _trigger_bg_ensure(c)                # 新自选 → 后台补数据
    except Exception as e:  # noqa: BLE001 —— 解析/类型错都转成前端可读提示
        return {"ok": False, "error": f"{fname} 解析失败：{type(e).__name__}: {e}"}

    return {"ok": True, "scope": scope, "table": name,
            "counts": counts, "bg_ensure": bg_new}
