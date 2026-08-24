"""FastAPI 应用工厂。uvicorn stockfu.api.server:app"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from stockfu.api import routes

_BASE = Path(__file__).resolve().parent.parent       # 包根 stockfu/(web/ 在包内)
_WEB_DIR = _BASE / "web"                             # 旧单页前端(web/index.html，share 卡片在此)
_FRONTEND_DIST = _BASE.parent / "frontend" / "dist"  # 项目根 frontend/(Vue 工程在包外)


def _has_spa() -> bool:
    """frontend/dist 是否已 build(Vue 版产物)。"""
    return (_FRONTEND_DIST / "index.html").exists()


def create_app() -> FastAPI:
    from stockfu.config import setup_network

    setup_network()  # 直接用 uvicorn 启动时也自动配代理
    app = FastAPI(title="StockFu·资产管理终端 API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(routes.router)

    # 新前端:dist 存在时托管 Vue build 产物(/assets 静态 + SPA fallback)。
    # 渐进策略:dist 在 → 用 Vue 版;不在 → 回落旧 web/index.html,不中断。
    spa = _has_spa()
    if spa:
        app.mount("/assets", StaticFiles(directory=str(_FRONTEND_DIST / "assets")), name="frontend-assets")

    # 进程级缓存 index.html:每请求 read_text 读盘无谓(2026-08-24 审查修复)。
    _index_html = (_FRONTEND_DIST if spa else _WEB_DIR).joinpath(
        "index.html").read_text(encoding="utf-8")

    @app.get("/", include_in_schema=False)
    def _index() -> HTMLResponse:
        """前端入口:Vue dist(若已 build)否则旧单页。"""
        return HTMLResponse(_index_html)

    if spa:
        @app.get("/{full:path}", include_in_schema=False)
        def _spa_fallback(full: str) -> HTMLResponse:
            """前端路由 fallback:未命中 API/静态的 GET 回 SPA。"""
            return HTMLResponse(_index_html)

    return app


app = create_app()
