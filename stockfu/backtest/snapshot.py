"""V2 回测数据快照（设计 §14、整改 §4.8.2）。

canonical 工件必须绑定不可变数据快照，不能把持续变化的主库当成可复现快照：

- ``create_data_snapshot`` 用 SQLite backup API 生成只读副本（WAL 合并后的
  一致性快照），``snapshot_id = sha256(快照文件)``——内容身份：日期/行数不变
  但值变化，ID 必然变化。
- 相同内容的快照幂等去重（同 ID 文件已存在则复用，不重复占盘）。
- descriptor 记录依赖表摘要（rows/max_date 只作审计；真正的身份来自文件内容）。
- ``validate_snapshot`` 校验 descriptor 与磁盘文件一致（恢复/复用前调用）。
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.pool import NullPool

# V2 vertical slice 的全部输入依赖表；新增 raw metric 时同步扩展。
DEPENDENCY_TABLES = (
    "quote_snapshot",        # 个股行情（OHLC/涨跌停/ST/amount/估值）
    "index_quote_daily",     # 指数行情（benchmark/regime）
    "etf_quote_daily",       # ETF 行情
    "dividend_event",        # 现金分红/送转事件
    "stock_basic",           # 股票基础/存续信息（listing/delisting/industry/is_st）
    "index_constituent",     # 历史指数成分（universe membership）
    "security_master",       # 点时存续/行业（UniverseContext.load 实读）
    # 财务三表 PIT（2026-08 质量因子 raw 依赖；快照整库备份天然含表，
    # 此处加入依赖表摘要供审计追溯）
    "financial_profit",      # 业绩报表（ROE/毛利率/净利/营收）
    "financial_balance",     # 资产负债表（总资产/负债率/权益）
    "financial_cashflow",    # 现金流量表（经营现金流）
    "financial_growth",      # 成长能力（同比，预留）
)

SNAPSHOT_DIR = Path(__file__).resolve().parents[2] / "data" / "snapshots"


def db_path() -> str:
    """主库 sqlite 文件路径；非 sqlite 主库直接拒绝（快照语义不适用）。"""
    from stockfu.db import engine

    url = str(engine.url)
    if url.startswith("sqlite:///"):
        return url[len("sqlite:///"):]
    raise ValueError(f"数据快照只支持 sqlite 主库，当前 url={url}")


def _table_summary(conn: sqlite3.Connection, table: str) -> dict:
    """表摘要（审计用）：有 quote_date 列则记录 max_date；无则只记行数；表缺失记 None。"""
    try:
        row = conn.execute(
            f"select max(quote_date), count(*) from {table}").fetchone()
        return {"rows": int(row[1]),
                "max_date": str(row[0]) if row[0] is not None else None}
    except sqlite3.OperationalError:
        try:
            n = conn.execute(f"select count(*) from {table}").fetchone()[0]
            return {"rows": int(n), "max_date": None}
        except sqlite3.OperationalError:
            return {"rows": None, "max_date": None}


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def create_data_snapshot(snapshots_dir: str | None = None,
                        src_path: str | None = None) -> dict:
    """备份主库到 ``data/snapshots/stockfu-<sha256前12>.db``，返回 descriptor。

    幂等：同内容快照文件已存在则复用（删除临时副本），不重复占盘。
    descriptor 的 rows/max_date 从快照文件自身读取（描述快照内容而非主库）。
    ``src_path`` 供单元测试注入临时 sqlite 库；默认使用主库。
    """
    src_path = src_path or db_path()
    out_dir = Path(snapshots_dir) if snapshots_dir else SNAPSHOT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    tmp = out_dir / f".tmp-{uuid.uuid4().hex}.db"
    try:
        src = sqlite3.connect(src_path)
        try:
            dst = sqlite3.connect(str(tmp))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        snapshot_id = _file_sha256(tmp)
        target = out_dir / f"stockfu-{snapshot_id[:12]}.db"
        if target.exists():
            tmp.unlink()
        else:
            os.replace(tmp, target)
        # 运行期不可变（§4.13.3-4）：移除全部写位，另一进程/连接无法原地修改或
        # 替换内容（回测 finalize 前还会再复验 SHA）。复用既有快照也幂等加固。
        os.chmod(target, 0o444)
    except BaseException:
        # backup/hash/落盘失败（如磁盘满）→ 清 tmp 孤儿，避免 GB 级 .tmp-* 残留占盘。
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise

    con = sqlite3.connect(str(target))
    try:
        tables = {t: _table_summary(con, t) for t in DEPENDENCY_TABLES}
    finally:
        con.close()
    rel = target.relative_to(Path.cwd()) if target.is_relative_to(Path.cwd()) else target
    return {
        "snapshot_id": f"sha256:{snapshot_id}",
        "path": str(rel),
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data_end": tables["quote_snapshot"]["max_date"],
        "file_size": target.stat().st_size,
        "tables": tables,
        # 实际日历来源：快照激活时 _trade_calendar_days 用 quote_snapshot
        # 的 distinct quote_date 构造（engine.py），不得误记为 akshare。
        "calendar_source": "quote_snapshot.distinct_quote_date",
    }


def validate_snapshot(snapshot: dict) -> None:
    """校验 descriptor 与磁盘快照一致：文件存在 + 内容 SHA-256 重算匹配。

    不一致（文件丢失/被改/descriptor 伪造）时抛 ValueError，不允许静默续跑。
    """
    if not isinstance(snapshot, dict) or not snapshot.get("snapshot_id"):
        raise ValueError("数据快照 descriptor 无效：缺少 snapshot_id")
    raw_id = str(snapshot["snapshot_id"])
    if not raw_id.startswith("sha256:"):
        raise ValueError(f"snapshot_id 必须为 sha256:<hex> 格式: {raw_id[:16]}…")
    path = Path(snapshot.get("path") or "")
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.exists():
        raise ValueError(f"数据快照文件不存在: {path}")
    actual = _file_sha256(path)
    if actual != raw_id[len("sha256:"):]:
        raise ValueError(
            f"数据快照内容与 descriptor 不一致（文件被修改或 descriptor 伪造）: {path}")


# snapshot_engine 按 path memoize：同一快照文件复用同一引擎对象，避免重复建连。
_SNAPSHOT_ENGINES: dict[str, Engine] = {}


def snapshot_engine(descriptor: dict) -> Engine:
    """快照文件只读 SQLAlchemy 引擎（按 path memoize）。

    V2 取数经 ``use_read_engine(snapshot_engine(descriptor))`` 切到它，保证整条
    取数链路读不可变快照而非主库。``mode=ro`` 文件级只读；connect 只设
    ``busy_timeout`` + ``query_only``——只读连接不能切 journal_mode，设 WAL 会
    ``SQLITE_READONLY``（不同于主库的 WAL pragma）。``NullPool`` 避免 memoized
    池在长测试套里累积连接。
    """
    rel = descriptor.get("path") or ""
    path = Path(rel)
    if not path.is_absolute():
        path = Path.cwd() / rel
    if not path.exists():
        raise ValueError(f"数据快照文件不存在: {path}")
    key = str(path)
    eng = _SNAPSHOT_ENGINES.get(key)
    if eng is not None:
        return eng
    ro_uri = f"file:{path}?mode=ro&uri=true"

    def _ro_creator():
        # creator 直出 sqlite3 连接，绕开 SQLAlchemy 对 file:...?mode=ro 的 URL 解析；
        # 与 services/recommend.py 的只读口径一致。
        return sqlite3.connect(ro_uri, uri=True, check_same_thread=False)

    eng = create_engine("sqlite://", creator=_ro_creator, poolclass=NullPool)

    @event.listens_for(eng, "connect")
    def _set_ro_pragma(dbapi_conn, _record):  # noqa: F811
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA query_only=ON")
        cur.close()

    _SNAPSHOT_ENGINES[key] = eng
    return eng


def clear_snapshot_engines() -> None:
    """释放所有 memoized 快照引擎（测试 teardown 用）。"""
    for eng in _SNAPSHOT_ENGINES.values():
        eng.dispose()
    _SNAPSHOT_ENGINES.clear()


def descriptor_from_file(path: str) -> dict:
    """从既有快照 .db 文件重建 descriptor（重算内容 sha256 + 依赖表摘要）。

    供 ``--snapshot PATH`` 复用已存在快照，或来源 checkpoint 绑定的快照文件路径
    失效（移动/换机/cwd 不同致相对路径错位）时兜底——内容 ``snapshot_id`` 匹配
    记录值才认，否则由调用方拒绝，绝不静默重建。
    """
    p = Path(path)
    if not p.exists():
        raise ValueError(f"快照文件不存在: {path}")
    snapshot_id = _file_sha256(p)
    con = sqlite3.connect(str(p))
    try:
        tables = {t: _table_summary(con, t) for t in DEPENDENCY_TABLES}
    finally:
        con.close()
    rel = p.relative_to(Path.cwd()) if p.is_relative_to(Path.cwd()) else p
    return {
        "snapshot_id": f"sha256:{snapshot_id}",
        "path": str(rel),
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data_end": tables.get("quote_snapshot", {}).get("max_date"),
        "file_size": p.stat().st_size,
        "tables": tables,
        "calendar_source": "quote_snapshot.distinct_quote_date",
    }
