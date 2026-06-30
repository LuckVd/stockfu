"""CSV 导入 / 导出（市场客观数据 <--> SQLite）。

通用、基于 SQLAlchemy metadata 反射，不逐表写死：
- export_csv：表 → data/<table>.csv（与 sqlite3 .mode csv 格式兼容，可入 git）
- import_csv：data/<table>.csv → 表（merge upsert，按主键/唯一约束合并，不删现有数据）

默认表集 = 市场客观数据（与 commit a6052f0 那批 CSV 一致）；all_tables=True 扩展到
个人交易/持仓/新闻/配置。feat/sector-fundflow 的 sector_* 表合并后经反射自动支持。

实现备注：表元数据统一从 ``SQLModel.metadata.tables`` 取（SQLAlchemy 注册表，稳定），
**不**走 ``cls.__table__``——后者在 py3.14 + pydantic 下经 ``inspect.getmembers``
访问会间歇性触发元类惰性报错；模型类则用 ``vars(models)`` 容错匹配，仅用于
``add`` / ``select``。

用法见 main.py 的 --export-csv / --import-csv。
"""
from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlmodel import SQLModel, select

from stockfu import models
from stockfu.db import session_scope

# 默认：市场客观数据（与 commit a6052f0 提交的那批 CSV 一致，可安全入 git/共享）
MARKET_TABLES = [
    "asset", "quote_snapshot", "dividend_event",
    "index_snapshot", "factor_snapshot", "fundflow_snapshot",
]
# all_tables=True 才包含：个人交易/持仓/新闻/本地配置（含敏感信息，不宜提交 git）
PERSONAL_TABLES = ["transaction", "holding", "news_item", "app_config"]

# 无主键/无唯一约束的表：显式业务键兜底，避免重复导入产生重复行
_FALLBACK_KEYS: dict[str, list[str]] = {
    "dividend_event": ["asset_code", "ex_date"],
    "transaction": ["asset_code", "side", "shares", "price", "trade_date"],
    "news_item": ["title", "url"],
}


def _table(name: str):
    """从 SQLModel registry 取 SQLAlchemy Table（单一真相源）。"""
    return SQLModel.metadata.tables[name]


def _model_classes() -> dict[str, type]:
    """表名 → SQLModel 类（vars 扫描，容错；仅用于 add/select）。"""
    by_name: dict[str, type] = {}
    for attr in vars(models).values():
        if isinstance(attr, type):
            tb = getattr(attr, "__tablename__", None)
            if tb and tb in SQLModel.metadata.tables:
                by_name[tb] = attr
    return by_name


def _resolve_tables(tables: list[str] | None, all_tables: bool) -> list[str]:
    if all_tables:
        return MARKET_TABLES + PERSONAL_TABLES
    return tables or MARKET_TABLES


def _columns(name: str) -> list[tuple[str, Any]]:
    """按定义顺序返回 (列名, SQLAlchemy Column)。"""
    return [(c.name, c) for c in _table(name).columns]


def _python_type(col) -> type:
    try:
        return col.type.python_type        # Date→date, DateTime→datetime, Boolean→bool …
    except (NotImplementedError, AttributeError):
        return str


def _issubclass_safe(t: type, base: type) -> bool:
    try:
        return issubclass(t, base)
    except TypeError:
        return False


def _is_autoincrement_pk(col) -> bool:
    """SQLite 整数单列主键视为自增 → 导入时丢弃该列，由 DB 重新分配。"""
    return bool(col.primary_key) and _issubclass_safe(_python_type(col), int)


def _natural_keys(name: str) -> list[str]:
    """upsert 去重键：非自增单列主键 > UniqueConstraint > 显式兜底。"""
    from sqlalchemy import UniqueConstraint

    table = _table(name)
    pk_cols = list(table.primary_key.columns)
    if len(pk_cols) == 1 and not _is_autoincrement_pk(pk_cols[0]):
        return [pk_cols[0].name]
    for c in table.constraints:
        if isinstance(c, UniqueConstraint) and len(c.columns) > 0:
            return [col.name for col in c.columns]
    if name in _FALLBACK_KEYS:
        return _FALLBACK_KEYS[name]
    raise ValueError(f"{name} 无可用的去重键（无主键/唯一约束/兜底定义）")


# ---- 序列化（导出：值 → 字符串，与现有 CSV 逐字节兼容）---------------------------

def _serialize(col, val: Any) -> str:
    if val is None:
        return ""
    pt = _python_type(col)
    if _issubclass_safe(pt, bool):                # bool 先于 int（bool 是 int 子类）
        return "1" if val else "0"
    if _issubclass_safe(pt, datetime):            # datetime 先于 date（datetime 是 date 子类）
        return val.isoformat(sep=" ")             # "YYYY-MM-DD HH:MM:SS.ffffff"
    if _issubclass_safe(pt, date):
        return val.isoformat()                    # "YYYY-MM-DD"
    return str(val)


# ---- 反序列化（导入：字符串 → 值）----------------------------------------------

def _deserialize(col, raw: str) -> Any:
    pt = _python_type(col)
    if _issubclass_safe(pt, str):                 # str 列：空串保留 ""，避免把 name 清成 None
        return raw
    if raw == "":                                 # 非 str 列：空串 → None
        return None
    if _issubclass_safe(pt, bool):
        return raw.strip() in ("1", "true", "True")
    if _issubclass_safe(pt, datetime):
        return datetime.fromisoformat(raw)
    if _issubclass_safe(pt, date):
        return date.fromisoformat(raw)
    if _issubclass_safe(pt, float):
        return float(raw)
    if _issubclass_safe(pt, int):
        return int(raw)
    return raw


# ---- 导出 ---------------------------------------------------------------------

def export_csv(out_dir: str | Path = "data", tables: list[str] | None = None,
               all_tables: bool = False) -> dict[str, int]:
    """把表导出为 <out_dir>/<table>.csv。返回 {表名: 行数}。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    model_map = _model_classes()
    result: dict[str, int] = {}
    with session_scope() as s:
        for name in _resolve_tables(tables, all_tables):
            if name not in SQLModel.metadata.tables:
                print(f"⚠ 跳过 {name}：无对应表（feat 分支表？合并后自动支持）")
                continue
            model = model_map[name]
            cols = _columns(name)
            headers = [c_name for c_name, _ in cols]
            rows = s.exec(select(model)).all()
            path = out / f"{name}.csv"
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(headers)
                for row in rows:
                    w.writerow([_serialize(col, getattr(row, c_name))
                                for c_name, col in cols])
            result[name] = len(rows)
            print(f"✓ 导出 {name}: {len(rows)} 行 → {path}")
    return result


# ---- 导入 ---------------------------------------------------------------------

def _import_one(name: str, path: Path) -> dict[str, int]:
    model = _model_classes()[name]
    cols = _columns(name)
    col_by_name = {n: c for n, c in cols}
    autoinc = {n for n, c in cols if _is_autoincrement_pk(c)}   # 导入时丢弃自增主键
    keys = _natural_keys(name)
    counts = {"inserted": 0, "updated": 0, "skipped": 0}

    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        file_cols = reader.fieldnames or []
        raw_rows = list(reader)

    # 解析每行：丢弃自增 id 与未知列，按列类型还原值
    parsed_rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        parsed = {n: _deserialize(col_by_name[n], raw.get(n, ""))
                  for n in file_cols if n not in autoinc and n in col_by_name}
        parsed_rows.append(parsed)

    with session_scope() as s:
        # 全量载入现有行：一次查询同时建「键集合」（判重）与「键→对象索引」（更新）。
        # 注：不用 select(键列) 投影——SQLModel 对单列 select 返回标量而非 Row，
        # tuple() 会把字符串键拆成字符元组导致判重失效。
        def _key_of(r):
            return tuple(getattr(r, k) for k in keys)

        existing_rows = s.exec(select(model)).all()
        existing_keys: set[tuple] = {_key_of(r) for r in existing_rows}

        to_insert: list[dict[str, Any]] = []
        to_update: list[dict[str, Any]] = []
        for parsed in parsed_rows:
            kt = tuple(parsed.get(k) for k in keys)
            if kt in existing_keys:
                to_update.append(parsed)
            else:
                to_insert.append(parsed)
                existing_keys.add(kt)            # 防止文件内重复键被当成多条新增

        for parsed in to_insert:                 # 新增：直接 add
            s.add(model(**parsed))
            counts["inserted"] += 1

        if to_update:                            # 更新：复用 existing_rows 建索引，逐个改字段
            existing_map: dict[tuple, Any] = {_key_of(r): r for r in existing_rows}
            for parsed in to_update:
                obj = existing_map.get(tuple(parsed.get(k) for k in keys))
                if obj is None:                  # 边角：当作新增
                    s.add(model(**parsed))
                    counts["inserted"] += 1
                    continue
                changed = False
                for n, val in parsed.items():
                    if getattr(obj, n) != val:
                        setattr(obj, n, val)
                        changed = True
                counts["updated" if changed else "skipped"] += 1
        s.commit()

    print(f"✓ 导入 {name}: "
          f"+{counts['inserted']} 新增  ~{counts['updated']} 更新  ={counts['skipped']} 跳过")
    return counts


def import_csv(in_dir: str | Path = "data", tables: list[str] | None = None,
               all_tables: bool = False) -> dict[str, dict[str, int]]:
    """从 <in_dir>/<table>.csv 合并导入（upsert）。返回 {表名: {inserted,updated,skipped}}。"""
    src = Path(in_dir)
    result: dict[str, dict[str, int]] = {}
    for name in _resolve_tables(tables, all_tables):
        if name not in SQLModel.metadata.tables:
            print(f"⚠ 跳过 {name}：无对应表（feat 分支表？合并后自动支持）")
            continue
        path = src / f"{name}.csv"
        if not path.exists():
            print(f"⚠ 跳过 {name}：{path} 不存在")
            continue
        result[name] = _import_one(name, path)
    return result
