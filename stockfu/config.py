"""全局配置（pydantic-settings，读取 .env）。

设计要点：所有可选项「不配置也能跑」——免费公开数据源作为默认能力，
配置 token/key 后增强质量。这与 daily_stock_analysis 的配置哲学一致。
"""
import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# /opt/pro/stockfu
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 基础
    app_name: str = "StockFu·资产管理终端"
    api_host: str = "127.0.0.1"
    api_port: int = 8787

    # 存储
    db_url: str = f"sqlite:///{DATA_DIR / 'stockfu.db'}"

    # 种子自选
    watchlist: str = "600519,000858,600036,601318,00700,09988,AAPL,MSFT,510300,510500"

    # 数据源增强
    tushare_token: str = ""

    # AI
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""

    # 定时
    daily_cron: str = "18 18 * * 1-5"

    # 网络：港美股(yfinance)走代理；国内数据源(akshare/efinance)直连
    proxy_url: str = "http://127.0.0.1:7890"
    proxy_bypass: str = ("eastmoney.com,sina.com.cn,qq.com,cninfo.com.cn,"
                         "localhost,127.0.0.1,local,.local")

    @property
    def watchlist_codes(self) -> list[str]:
        return [c.strip() for c in self.watchlist.split(",") if c.strip()]

    def ensure_dirs(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()


def setup_network() -> None:
    """配置代理：yfinance 走代理(在 yfinance_source 内注入 proxy session)，国内源直连。

    重要：**不**设全局 HTTP_PROXY 环境变量——efinance/akshare 底层 session 一旦
    在 import/调用时见到全局代理就会误用(SSL EOF / InvalidSchema)，导致 A 股源
    全部失败、fallback 到 yfinance 返回港股脏数据(如 002594 显示 'BYD COMPANY LI')。
    故代理仅在 yfinance_source._proxy_session() 内按需注入；国内源全程无代理直连。
    no_proxy 仍设(防御外部已预设的代理)。
    """
    if settings.proxy_bypass:
        for k in ("no_proxy", "NO_PROXY"):
            os.environ.setdefault(k, settings.proxy_bypass)
