"""FastAPI 应用工厂。uvicorn stockfu.api.server:app"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from stockfu.api import routes

_WEB_DIR = Path(__file__).resolve().parent.parent / "web"


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

    @app.get("/", include_in_schema=False)
    def _index() -> HTMLResponse:
        """前端单页（托管在 FastAPI，免单独起前端服务）。"""
        return HTMLResponse((_WEB_DIR / "index.html").read_text(encoding="utf-8"))

    return app


app = create_app()
