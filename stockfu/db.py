"""数据库引擎 / 会话 / 建表 / 迁移 / 种子数据。"""
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date

from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

from stockfu import models  # noqa: F401  —— 注册所有表
from stockfu.config import settings
from stockfu.data.base import (classify_asset_type, currency_of, detect_market,
                            normalize_stock_code)

engine = create_engine(
    settings.db_url,
    echo=False,
    connect_args={"check_same_thread": False},
)


def _migrate() -> None:
    """开发期迁移：
    1) 旧 index_snapshot 缺 scope/level 列则重建（仅丢指数数据，可 --fetch 重算）；
    2) quote_snapshot 补 turnover 列（SQLModel create_all 不改已有表）。
    不动 quote/dividend/holding 数据。"""
    insp = inspect(engine)
    if insp.has_table("index_snapshot"):
        cols = [c["name"] for c in insp.get_columns("index_snapshot")]
        if "scope" not in cols or "level" not in cols:
            with engine.begin() as conn:
                conn.execute(text("DROP TABLE index_snapshot"))
    if insp.has_table("quote_snapshot"):
        cols = [c["name"] for c in insp.get_columns("quote_snapshot")]
        if "turnover" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE quote_snapshot ADD COLUMN turnover FLOAT"))


def init_db() -> None:
    """建表（幂等，含迁移）。"""
    _migrate()
    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    with Session(engine) as s:
        yield s


@contextmanager
def session_scope() -> Iterator[Session]:
    with Session(engine) as s:
        yield s


def seed_samples() -> list[str]:
    """把配置里的 watchlist 写入 asset 表（is_watch=True）。"""
    from stockfu.models import Asset

    added: list[str] = []
    with Session(engine) as s:
        for raw in settings.watchlist_codes:
            code = normalize_stock_code(raw)
            if s.get(Asset, code):
                continue
            market = detect_market(code)
            s.add(
                Asset(
                    code=code,
                    name="",
                    market=market,
                    asset_type=classify_asset_type(code, market),
                    currency=currency_of(market),
                    is_watch=True,
                )
            )
            added.append(code)
        s.commit()
    return added


def seed_demo_holdings() -> list[str]:
    """写入演示持仓（方便首次看板有内容；非真实交易，清空 holding 表即可移除）。"""
    from stockfu.models import Holding

    demos = [
        ("600519", 100, 1500.0),   # 贵州茅台
        ("510300", 5000, 4.20),    # 沪深300ETF
        ("600036", 1000, 38.0),    # 招商银行
        ("HK00700", 200, 380.0),   # 腾讯
        ("AAPL", 50, 240.0),       # 苹果
    ]
    codes: list[str] = []
    with Session(engine) as s:
        for code, shares, cost in demos:
            if s.get(Holding, code):
                continue
            s.add(Holding(
                asset_code=code, shares=shares, avg_cost=cost,
                total_cost=round(shares * cost, 2),
                first_buy_date=date(2024, 1, 15),
            ))
            codes.append(code)
        s.commit()
    return codes


def _ensure_tables() -> None:
    """幂等建表（create_all 只建缺失表，不动已有数据）。

    server 启动时不调 init_db，故运行时新表（如 app_config）在此 lazy 生效。
    """
    SQLModel.metadata.create_all(engine)


def has_app_config(key: str) -> bool:
    from stockfu.models import AppConfig
    _ensure_tables()
    with session_scope() as s:
        return s.get(AppConfig, key) is not None


def get_app_config(key: str, default: str = "") -> str:
    from stockfu.models import AppConfig
    _ensure_tables()
    with session_scope() as s:
        row = s.get(AppConfig, key)
        return row.value if row else default


def set_app_config(key: str, value: str) -> None:
    from stockfu.models import AppConfig
    _ensure_tables()
    with session_scope() as s:
        row = s.get(AppConfig, key)
        if row is None:
            s.add(AppConfig(key=key, value=value))
        else:
            row.value = value
        s.commit()
