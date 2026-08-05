"""CSV 导入 / 导出（市场客观数据 <--> SQLite）。

通用、基于 SQLAlchemy metadata 反射，不逐表写死：
- export_csv：表 → data/<table>.csv（与 sqlite3 .mode csv 格式兼容，可入 git）
- import_csv：data/<table>.csv → 表（merge upsert，按主键/唯一约束合并，不删现有数据）

默认表集 = 市场客观数据（与现有市场 CSV 集合一致）；all_tables=True 扩展到
个人交易/持仓/新闻/配置。feat/sector-fundflow 的 sector_* 表合并后经反射自动支持。

实现备注：表元数据统一从 ``SQLModel.metadata.tables`` 取（SQLAlchemy 注册表，稳定），
**不**走 ``cls.__table__``——后者在 py3.14 + pydantic 下经 ``inspect.getmembers``
访问会间歇性触发元类惰性报错；模型类则用 ``vars(models)`` 容错匹配，仅用于
``add`` / ``select``。

用法见 main.py 的 --export-csv / --import-csv。
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlmodel import SQLModel, select

from stockfu import models
from stockfu.db import session_scope

# 默认：市场客观数据（可安全入 git/共享）
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

# ---- WebUI 工具栏的语义范围 → 表 ----------------------------------------------
# 自选 = 追踪股票清单(asset 表)；持仓 = 交易流水(transaction 表，holding 由其移动加权派生)。
# 每个 scope 对应单表 → 单 CSV 文件，上传/下载/模板 UX 干净。
SCOPE_TABLES: dict[str, list[str]] = {
    "holdings": ["transaction"],
    "watchlist": ["asset"],
}

# 导入模板：中文表头 + 列顺序（只给表头，无示例行）。
#   持仓去掉 amount/fee（amount 导入时按 shares*price 自动补；fee 默认 0）；trade_date 选填。
TEMPLATE_COLS: dict[str, list[str]] = {
    "transaction": ["asset_code", "side", "shares", "price", "trade_date", "note"],
    "asset": ["code", "name", "market", "asset_type", "sector", "currency", "is_watch", "note"],
}

# 英文列名 → 中文表头（模板下载与界面展示用）；导入时反向映射中文表头→英文列名。
COLUMN_CN: dict[str, dict[str, str]] = {
    "transaction": {"asset_code": "代码", "side": "方向", "shares": "股数", "price": "价格",
                    "amount": "成交额", "fee": "手续费", "trade_date": "日期", "note": "备注"},
    "asset": {"code": "代码", "name": "名称", "market": "市场", "asset_type": "类型",
              "sector": "板块", "currency": "币种", "is_watch": "自选", "note": "备注"},
}

# 每表的主键列（必填）；表头识别失败时用它给清晰提示，而非抛 NOT NULL。
REQUIRED_COL = {"transaction": "asset_code", "asset": "code"}


def normalize_text(text: str) -> str:
    """清洗上传文本：去 UTF-8 BOM；全角逗号 U+FF0C → 半角；分号分隔→逗号（部分 Excel 区域设置）。"""
    text = text.replace("﻿", "").replace("，", ",")
    first = text.splitlines()[0] if text else ""
    if "," not in first and ";" in first:            # 分号分隔（欧洲/部分中文 Excel 默认）
        text = text.replace(";", ",")
    return text


def resolve_scope(scope: str) -> list[str]:
    """WebUI 语义范围 → 表名列表。未知 scope 抛 ValueError（端点转 400）。"""
    if scope not in SCOPE_TABLES:
        raise ValueError(f"未知范围 '{scope}'，可选：{', '.join(SCOPE_TABLES)}")
    return SCOPE_TABLES[scope]


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

_DATE_RE = re.compile(r"(\d{4})\D(\d{1,2})\D(\d{1,2})")


def _parse_date(raw: str) -> date:
    """容错日期解析：接受 2024-03-20 / 2024/3/20 / 2024.3.20 / 2024-3-20 10:00:00。"""
    m = _DATE_RE.search(raw.strip())
    if m:
        return date(int(m[1]), int(m[2]), int(m[3]))
    return date.fromisoformat(raw.strip()[:10])    # 兜底（仍可能抛，交由端点转可读错误）


def _parse_datetime(raw: str) -> datetime:
    """容错 datetime 解析：带时间用 fromisoformat；纯日期补 00:00:00。"""
    s = raw.strip().replace("/", "-")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.combine(_parse_date(s), datetime.min.time())


def _deserialize(col, raw: str) -> Any:
    pt = _python_type(col)
    if _issubclass_safe(pt, str):                 # str 列：空串保留 ""，避免把 name 清成 None
        return raw
    if raw == "":                                 # 非 str 列：空串 → None
        return None
    if _issubclass_safe(pt, bool):                # 是/yes/Y/✓ 也算 True
        return raw.strip() in ("1", "true", "True", "是", "yes", "Y", "y", "✓")
    if _issubclass_safe(pt, datetime):
        return _parse_datetime(raw)
    if _issubclass_safe(pt, date):
        return _parse_date(raw)
    if _issubclass_safe(pt, float):
        return float(raw.strip().replace(",", ""))   # 容忍千分位 "1,500"
    if _issubclass_safe(pt, int):
        return int(raw.strip().replace(",", ""))
    return raw


# ---- 导出 ---------------------------------------------------------------------

def export_table_text(name: str) -> tuple[str, int]:
    """单表全量导出为 CSV 文本（与 data/<table>.csv 逐字节一致）。返回 (文本, 数据行数)。

    供 WebUI 下载用；csv.writer 默认 CRLF 行尾原样保留。
    """
    model = _model_classes()[name]
    cols = _columns(name)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([c_name for c_name, _ in cols])
    n = 0
    with session_scope() as s:
        for row in s.exec(select(model)).all():
            w.writerow([_serialize(col, getattr(row, c_name)) for c_name, col in cols])
            n += 1
    return buf.getvalue(), n


def template_text(name: str) -> str:
    """生成 CSV 模板：只含中文表头一行（按 TEMPLATE_COLS 顺序），无示例数据。"""
    cn = COLUMN_CN[name]
    headers = [cn[c] for c in TEMPLATE_COLS[name]]
    buf = io.StringIO()
    csv.writer(buf).writerow(headers)
    return buf.getvalue()


def alias_headers(name: str, text: str) -> str:
    """把 CSV 表头里的中文别名换成英文列名（数据行不动）；英文表头原样保留。

    模板用中文表头，导入需还原成模型列名才能 upsert。
    """
    cn_to_en = {cn: en for en, cn in COLUMN_CN[name].items()}
    reader = csv.DictReader(io.StringIO(text))
    fields = reader.fieldnames or []
    rows = list(reader)
    mapped = [cn_to_en.get(f.strip(), f) for f in fields]
    if mapped == fields:
        return text                       # 无中文表头（如导出文件直入），原样返回
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(mapped)
    for r in rows:
        w.writerow([r.get(f, "") for f in fields])
    return buf.getvalue()


def export_csv(out_dir: str | Path = "data", tables: list[str] | None = None,
               all_tables: bool = False) -> dict[str, int]:
    """把表导出为 <out_dir>/<table>.csv。返回 {表名: 行数}。CLI --export-csv 用。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    result: dict[str, int] = {}
    for name in _resolve_tables(tables, all_tables):
        if name not in SQLModel.metadata.tables:
            print(f"⚠ 跳过 {name}：无对应表（feat 分支表？合并后自动支持）")
            continue
        text, n = export_table_text(name)
        path = out / f"{name}.csv"
        path.write_bytes(text.encode("utf-8"))   # 原样落盘，不做换行翻译
        result[name] = n
        print(f"✓ 导出 {name}: {n} 行 → {path}")
    return result


# ---- 导入 ---------------------------------------------------------------------

def fill_transaction_amount(text: str) -> str:
    """确保 transaction CSV 含正确的 amount = shares * price。

    模板省略了 amount 列（及 amount 列留空时）：此处按 shares*price 补出该列；
    移动加权成本(recompute_holding)依赖此值，否则成本会算成 0。
    """
    reader = csv.DictReader(io.StringIO(text))
    fields = list(reader.fieldnames or [])
    if not ({"shares", "price"} <= set(fields)):
        return text                            # 缺 shares/price 无法算，原样返回
    rows = list(reader)
    out_fields = fields + ([] if "amount" in fields else ["amount"])
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(out_fields)
    for r in rows:
        amt = (r.get("amount") or "").strip()
        try:
            sh = float((r.get("shares") or "").strip().replace(",", "") or 0)
            pr = float((r.get("price") or "").strip().replace(",", "") or 0)
        except ValueError:
            sh = pr = 0.0
        if (not amt or amt in ("0", "0.0", "0.00")) and sh and pr:
            r["amount"] = str(round(sh * pr, 2))
        w.writerow([r.get(f, "") for f in out_fields])
    return buf.getvalue()


# 方向列（side）中文值 → 英文枚举。recompute_holding 只认 buy/sell/dividend。
_SIDE_CN = {"买入": "buy", "买": "buy", "买进": "buy", "加仓": "buy",
            "卖出": "sell", "卖": "sell", "卖出平仓": "sell", "减仓": "sell",
            "分红": "dividend", "股息": "dividend", "派息": "dividend"}


def normalize_side_values(text: str) -> str:
    """把 transaction CSV 里 side 列的中文值（买入/卖出/分红…）换成 buy/sell/dividend。

    英文值原样保留；只在 side 列存在时生效。"""
    reader = csv.DictReader(io.StringIO(text))
    fields = reader.fieldnames or []
    if "side" not in fields:
        return text
    rows = list(reader)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(fields)
    for r in rows:
        v = (r.get("side") or "").strip()
        if v in _SIDE_CN:
            r["side"] = _SIDE_CN[v]
        w.writerow([r.get(f, "") for f in fields])
    return buf.getvalue()


def _import_rows(name: str, reader: csv.DictReader) -> dict[str, int]:
    model = _model_classes()[name]
    cols = _columns(name)
    col_by_name = {n: c for n, c in cols}
    autoinc = {n for n, c in cols if _is_autoincrement_pk(c)}   # 导入时丢弃自增主键
    keys = _natural_keys(name)
    counts = {"inserted": 0, "updated": 0, "skipped": 0}

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


def _import_one(name: str, path: Path) -> dict[str, int]:
    """从文件合并导入单表（CLI --import-csv 用）。"""
    with open(path, newline="", encoding="utf-8") as f:
        return _import_rows(name, csv.DictReader(f))


def import_table_text(name: str, text: str) -> dict[str, int]:
    """从 CSV 文本合并导入单表（upsert）。供 WebUI 上传用。"""
    return _import_rows(name, csv.DictReader(io.StringIO(text)))


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
