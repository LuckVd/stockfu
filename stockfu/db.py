"""数据库引擎 / 会话 / 建表 / 迁移 / 种子数据。"""
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date

from sqlalchemy import event, inspect, text
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


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_conn, _record):
    """每连接设 SQLite pragma(G09 性能优化):

    - busy_timeout 先设:journal_mode 切 WAL 需短暂写锁,先挂超时避免首连竞争 SQLITE_BUSY;
      多进程(scheduler 写 / 回测读)写竞争时等 5s 而非立即报错。
    - journal_mode=WAL:DB header 持久(首连设一次即持久,后续连接幂等返回 'wal');
      读不阻塞写、写不阻塞读,冷启动批量写缓存省 fsync。
    - synchronous=NORMAL:WAL 下 commit 不强制 fsync(WAL 刷盘由 checkpoint 兜底),
      冷启动写快一个量级;FULL(=2)在 WAL 模式下无额外安全收益。

    副作用:产生 data/stockfu.db-wal / -shm 旁路文件。备份/搬迁前先
    `PRAGMA wal_checkpoint(TRUNCATE)` 把 -wal 并回主库,即可照旧单文件拷贝
    (见 CLAUDE.md / docs/BACKTEST.md 性能段)。
    """
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA busy_timeout=5000")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


def _migrate() -> None:
    """开发期迁移：
    1) 旧 index_snapshot 缺 scope/level 列则重建（仅丢指数数据，可 --fetch 重算）；
    2) quote_snapshot 补 turnover 列（SQLModel create_all 不改已有表）；
    3) operator_result 删已废弃的 raw_score 列（G10 后 raw_score 并入 score，全库无代码
       读写；SQLite≥3.35 DROP COLUMN，幂等）；
    4) operator_result 删 4 个冗余单列索引（复合唯一键 uq_op_result_code_date_op_fp
       已覆盖全部热路径查询：全键等值 + asset_code 前导的 IN/Between）。孤儿清理
       （seed.py）改全表扫，罕见可接受。
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
        # 三套复权 OHLC:前复权(qfq)/不复权(raw)/后复权(hfq)。遗留 open/high/low/close ≡ qfq。
        adj_cols = [
            "open_qfq", "high_qfq", "low_qfq", "close_qfq",
            "open_raw", "high_raw", "low_raw", "close_raw",
            "open_hfq", "high_hfq", "low_hfq", "close_hfq",
        ]
        missing_adj = [c for c in adj_cols if c not in cols]
        if missing_adj:
            with engine.begin() as conn:
                for c in missing_adj:
                    conn.execute(text(f"ALTER TABLE quote_snapshot ADD COLUMN {c} FLOAT"))
            # 把历史前复权价拷到显式 *_qfq(仅空槽;不覆盖已回补)
            with engine.begin() as conn:
                conn.execute(text(
                    "UPDATE quote_snapshot SET "
                    "open_qfq = COALESCE(open_qfq, open), "
                    "high_qfq = COALESCE(high_qfq, high), "
                    "low_qfq = COALESCE(low_qfq, low), "
                    "close_qfq = COALESCE(close_qfq, close) "
                    "WHERE close IS NOT NULL OR close_qfq IS NOT NULL"
                ))
    if insp.has_table("operator_result"):
        cols = [c["name"] for c in insp.get_columns("operator_result")]
        # raw_score 列已废弃(G10 后并入 score,全库无代码读写)→ DROP 回收(SQLite≥3.35)。幂等。
        if "raw_score" in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE operator_result DROP COLUMN raw_score"))
        # 删 4 个冗余单列索引（复合唯一键最左前缀/前导列已覆盖）。DROP IF EXISTS 幂等。
        existing = {ix["name"] for ix in insp.get_indexes("operator_result")}
        redundant = ["ix_operator_result_asset_code", "ix_operator_result_as_of",
                     "ix_operator_result_operator_id", "ix_operator_result_fingerprint"]
        to_drop = [name for name in redundant if name in existing]
        if to_drop:
            with engine.begin() as conn:
                for name in to_drop:
                    conn.execute(text(f"DROP INDEX IF EXISTS {name}"))

    # asset.note 历史遗留列(早期 model 有,后移除)→ DROP 回收:当前 Asset 模型无此字段,
    # seed_samples INSERT 不带 note 会触发 NOT NULL 约束失败(干净库不受影响——create_all
    # 按当前 model 建表无 note;仅升级旧库命中)。SQLite≥3.35,幂等。
    if insp.has_table("asset"):
        cols = [c["name"] for c in insp.get_columns("asset")]
        if "note" in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE asset DROP COLUMN note"))

    # source hash 上线(P2-5):指纹纳入算子源码后,旧指纹全失效成孤儿占空间。
    # 一次性清空 operator_result(math 重算廉价,首次回测慢一次);幂等:标记设后不再清。
    if insp.has_table("operator_result") and not has_app_config("opcache_source_hash_migrated"):
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM operator_result"))
        set_app_config("opcache_source_hash_migrated", "1")


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
