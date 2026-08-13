"""baostock 财务三表 PIT 回补（分段 + 每日配额 + 断点续传，2026-08）。

背景约束（已确认）：
- baostock 每日调用上限约 5 万次，超出进黑名单 → 默认每日配额 40000 次（留余量）。
- 不支持并发连接 → 全程串行（单 IP 单 session）。
- 单次调用只返回单季单接口（实测），按 (code, year, quarter) 逐次调用。

流程：
1. 预取上市日期：query_stock_basic(code) → 回填 stock_basic.listing_date（幂等）。
   之后主回补按上市年份过滤，新股不拉上市前的年份。
2. 主回补：遍历 (code × 有效年份 × 1-4 季 × 接口)，
   - checkpoint(task_key="financial-v1", scope_key=接口名, item_key="code:year:quarter")
   - 每日配额写 data/financial_daily_count.json（跨进程防呆，超限直接退出）
   - error_code="0" 且 0 行 → 视为"该期无数据"，标 success 不再重试
   - 连续失败 N 次 → rotate_proxy 换代理重登

用法（main.py 入口）：
  python3 main.py --backfill-financial                          # 全量（默认预算 40000/天）
  python3 main.py --backfill-financial --fin-interfaces profit,balance
  python3 main.py --backfill-financial --fin-budget 5000
  python3 main.py --backfill-financial --fin-prefetch           # 只预取上市日期
  python3 main.py --backfill-financial --fin-status             # 只打印进度统计
"""
from __future__ import annotations

import json
import logging
import random
import time
from datetime import date, datetime
from pathlib import Path

from sqlmodel import select

from stockfu.db import session_scope
from stockfu.models import (BackfillCheckpoint, FinancialBalance, FinancialCashflow,
                            FinancialDupont, FinancialGrowth, FinancialOperation,
                            FinancialProfit, QuoteSnapshot, SecurityMaster)

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DAILY_COUNT_FILE = ROOT / "data" / "financial_daily_count.json"

TASK_KEY = "financial-v1"
DEFAULT_BUDGET = 40_000          # 每日调用上限（5 万硬限，留 20% 余量）
DEFAULT_YEAR_FROM = 2007         # baostock 财务数据最早约 2007（实测 2006 前无数据）
MAX_CONSECUTIVE_FAILURES = 5     # 连续失败后换代理

# 接口定义：名称 → (baostock 函数名, 模型类, 字段映射 {源字段: 模型字段})
# 源字段名以 baostock 文档为准；运行时按 rs.fields 动态取，映射外的字段跳过。
INTERFACES: dict[str, tuple[str, type, dict[str, str]]] = {
    "profit": ("query_profit_data", FinancialProfit, {
        "pubDate": "pub_date", "statDate": "stat_date",
        "roeAvg": "roe_avg", "npMargin": "np_margin", "gpMargin": "gp_margin",
        "netProfit": "net_profit", "epsTTM": "eps_ttm", "MBRevenue": "mb_revenue",
        "totalShare": "total_share", "liqaShare": "liqa_share",
    }),
    "growth": ("query_growth_data", FinancialGrowth, {
        "pubDate": "pub_date", "statDate": "stat_date",
        "YOYEquity": "yoy_equity", "YOYAsset": "yoy_asset", "YOYNI": "yoy_ni",
        "YOYEPSBasic": "yoy_eps_basic", "YOYPNI": "yoy_pni",
    }),
    "balance": ("query_balance_data", FinancialBalance, {
        "pubDate": "pub_date", "statDate": "stat_date",
        "currentRatio": "current_ratio", "quickRatio": "quick_ratio",
        "cashRatio": "cash_ratio", "YOYLiability": "yoy_liability",
        "liabilityToAsset": "liability_to_asset", "assetToEquity": "asset_to_equity",
    }),
    "operation": ("query_operation_data", FinancialOperation, {
        "pubDate": "pub_date", "statDate": "stat_date",
        "NRTurnRatio": "nr_turn_ratio", "NRTurnDays": "nr_turn_days",
        "INVTurnRatio": "inv_turn_ratio", "INVTurnDays": "inv_turn_days",
        "CATurnRatio": "ca_turn_ratio", "ASSETTurnRatio": "asset_turn_ratio",
    }),
    "cashflow": ("query_cash_flow_data", FinancialCashflow, {
        "pubDate": "pub_date", "statDate": "stat_date",
        "CAToAsset": "ca_to_asset", "NCAToAsset": "nca_to_asset",
        "tangibleAssetToAsset": "tangible_asset_to_asset",
        "ebitToInterest": "ebit_to_interest",
        "CFOToOR": "cfo_to_or", "CFOToNP": "cfo_to_np", "CFOToGr": "cfo_to_gr",
    }),
    "dupont": ("query_dupont_data", FinancialDupont, {
        "pubDate": "pub_date", "statDate": "stat_date",
        "dupontROE": "dupont_roe", "dupontAssetStoEquity": "dupont_asset_sto_equity",
        "dupontAssetTurn": "dupont_asset_turn", "dupontPnitoni": "dupont_pnitoni",
        "dupontNitogr": "dupont_nitogr", "dupontTaxBurden": "dupont_tax_burden",
        "dupontIntburden": "dupont_intburden", "dupontEbittogr": "dupont_ebittogr",
    }),
}


def _bs_code(code: str) -> str:
    return ("sh." if code[0] in ("6", "9", "5") else "sz.") + code


def _f(v: str) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _d(v: str) -> date | None:
    try:
        return datetime.strptime(str(v).strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _import_bs():
    import baostock  # noqa: PLC0415  # 未装时该源不可用
    return baostock


def _ensure_login() -> bool:
    from stockfu.data.baostock_source import BaostockSource  # noqa: PLC0415
    return BaostockSource._ensure_login()


def _rotate_proxy() -> bool:
    from stockfu.data.baostock_source import BaostockSource  # noqa: PLC0415
    return BaostockSource.rotate_proxy("financial_backfill")


# ---------- 每日配额（跨进程防呆） ----------

def _read_daily_count() -> tuple[str, int]:
    try:
        data = json.loads(DAILY_COUNT_FILE.read_text())
        if data.get("date") == date.today().isoformat():
            return data["date"], int(data.get("count", 0))
    except (FileNotFoundError, ValueError, KeyError):
        pass
    return date.today().isoformat(), 0


def _bump_daily_count(n: int = 1) -> int:
    today, count = _read_daily_count()
    count += n
    DAILY_COUNT_FILE.write_text(json.dumps({"date": today, "count": count}))
    return count


# ---------- 上市日期预取 ----------

def prefetch_listing_dates(codes: list[str]) -> int:
    """query_stock_basic → 回填 stock_basic.listing_date（幂等，已填的跳过）。"""
    if not _ensure_login():
        log.error("baostock 登录失败，跳过上市日期预取")
        return 0
    bs = _import_bs()
    with session_scope() as s:
        existing = {r.code for r in s.exec(
            select(SecurityMaster).where(SecurityMaster.list_date.is_not(None))).all()}
    todo = [c for c in codes if c not in existing]
    done = 0
    for i, code in enumerate(todo):
        try:
            rs = bs.query_stock_basic(code=_bs_code(code))
            row = None
            while (rs.error_code == "0") and rs.next():
                row = dict(zip(rs.fields, rs.get_row_data()))
            if row and row.get("ipoDate"):
                with session_scope() as s:
                    sm = s.exec(select(SecurityMaster).where(
                        SecurityMaster.code == code)).first()
                    if sm is None:
                        sm = SecurityMaster(code=code, name=row.get("code_name", ""))
                        s.add(sm)
                    sm.list_date = _d(row["ipoDate"])
                    sm.delist_date = _d(row["outDate"]) if row.get("outDate") else None
                    sm.status = "1" if row.get("status") == "1" else "0"
                    s.commit()
                done += 1
        except Exception:  # noqa: BLE001
            log.warning("ipoDate 预取失败: %s", code)
        time.sleep(random.uniform(0.15, 0.3))
        if (i + 1) % 200 == 0:
            log.info("ipoDate 预取 %d/%d", i + 1, len(todo))
    log.info("ipoDate 预取完成: %d 只（新增 %d）", len(todo), done)
    return done


# ---------- 主回补 ----------

def _stock_codes() -> list[str]:
    """quote_snapshot 中的 A 股代码（0/3/6 开头），排序稳定。"""
    with session_scope() as s:
        codes = {r[0] for r in s.exec(
            select(QuoteSnapshot.asset_code).distinct()).all()}
    return sorted(c for c in codes if c and c[0] in ("0", "3", "6"))


def _listing_year(code: str) -> int | None:
    with session_scope() as s:
        sm = s.exec(select(SecurityMaster).where(SecurityMaster.code == code)).first()
    if sm and sm.list_date:
        return sm.list_date.year
    return None


def _checkpointed(task_key: str, scope_key: str, item_key: str) -> bool:
    with session_scope() as s:
        return s.exec(select(BackfillCheckpoint).where(
            BackfillCheckpoint.task_key == task_key,
            BackfillCheckpoint.scope_key == scope_key,
            BackfillCheckpoint.item_key == item_key)).first() is not None


def _mark_done(task_key: str, scope_key: str, item_key: str) -> None:
    with session_scope() as s:
        cp = BackfillCheckpoint(task_key=task_key, scope_key=scope_key,
                                item_key=item_key, status="success", attempts=0)
        s.add(cp)
        s.commit()


def _plan(codes: list[str], interfaces: list[str],
          year_from: int, year_to: int) -> list[tuple[str, str, str, int, int]]:
    """生成 (接口, code, scope_key, year, quarter) 计划；按接口优先、股票顺序排列。"""
    plan: list[tuple[str, str, str, int, int]] = []
    for iface in interfaces:
        for code in codes:
            ly = _listing_year(code) or year_from
            start = max(year_from, ly)
            for year in range(start, year_to + 1):
                for quarter in range(1, 5):
                    item = f"{code}:{year}:{quarter}"
                    if not _checkpointed(TASK_KEY, iface, item):
                        plan.append((iface, code, item, year, quarter))
    return plan


def backfill_financial(interfaces: list[str] | None = None, codes: list[str] | None = None,
                       daily_budget: int = DEFAULT_BUDGET, year_from: int = DEFAULT_YEAR_FROM,
                       sleep_range: tuple[float, float] = (0.15, 0.3)) -> dict:
    """主回补。返回统计 dict：{接口: {done, empty, failed, skipped}}。"""
    ifaces = interfaces or list(INTERFACES)
    unknown = [i for i in ifaces if i not in INTERFACES]
    if unknown:
        raise ValueError(f"未知接口: {unknown}；可选: {list(INTERFACES)}")

    today, used = _read_daily_count()
    if today == date.today().isoformat() and used >= daily_budget:
        log.info("今日配额已用尽（%d/%d），退出。", used, daily_budget)
        return {"quota_exhausted": True, "used": used}

    bs = _import_bs()
    if not _ensure_login():
        raise RuntimeError("baostock 登录失败")

    all_codes = codes or _stock_codes()
    year_to = date.today().year
    plan = _plan(all_codes, ifaces, year_from, year_to)
    log.info("计划 %d 次调用（%d 只股票 × %d 接口 × 有效年份 × 4 季），今日已用 %d/%d",
             len(plan), len(all_codes), len(ifaces), used, daily_budget)

    stats = {i: {"done": 0, "empty": 0, "failed": 0, "skipped": 0} for i in ifaces}
    consecutive_failures = 0

    for idx, (iface, code, item, year, quarter) in enumerate(plan, 1):
        _, used = _read_daily_count()
        if used >= daily_budget:
            log.info("到达每日配额 %d，提前停止（已完成 %d/%d）。", daily_budget, idx - 1, len(plan))
            break

        func_name, model, mapping = INTERFACES[iface]
        func = getattr(bs, func_name)
        try:
            rs = func(code=_bs_code(code), year=year, quarter=quarter)
        except Exception:  # noqa: BLE001
            rs = None

        _bump_daily_count()
        if rs is None or rs.error_code != "0":
            stats[iface]["failed"] += 1
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                log.warning("连续 %d 次失败，换代理重登", consecutive_failures)
                _rotate_proxy()
                consecutive_failures = 0
            continue

        rows = []
        while rs.next():
            rows.append(dict(zip(rs.fields, rs.get_row_data())))
        if not rows:
            stats[iface]["empty"] += 1
            _mark_done(TASK_KEY, iface, item)   # 无数据也标记完成，避免重复调用
            consecutive_failures = 0
            continue

        with session_scope() as s:
            for row in rows:
                vals = {"asset_code": code, "year": year, "quarter": quarter}
                for src_field, model_field in mapping.items():
                    if src_field in row:
                        raw = row[src_field]
                        if model_field in ("pub_date", "stat_date"):
                            vals[model_field] = _d(raw)
                        else:
                            vals[model_field] = _f(raw)
                ex = s.exec(select(model).where(
                    model.asset_code == code, model.year == year,
                    model.quarter == quarter)).first()
                if ex is None:
                    s.add(model(**vals))
            s.commit()
        _mark_done(TASK_KEY, iface, item)
        stats[iface]["done"] += 1
        consecutive_failures = 0

        if idx % 500 == 0:
            log.info("进度 %d/%d | %s", idx, len(plan),
                     {k: v["done"] for k, v in stats.items()})
        time.sleep(random.uniform(*sleep_range))

    _, used = _read_daily_count()
    total_done = sum(v["done"] for v in stats.values())
    log.info("回补结束: 今日累计调用 %d，新完成 %d | %s", used, total_done,
             {k: v["done"] for k, v in stats.items()})
    return {"used": used, "stats": stats}


def financial_status() -> dict:
    """只读进度统计。"""
    with session_scope() as s:
        cps = s.exec(select(BackfillCheckpoint).where(
            BackfillCheckpoint.task_key == TASK_KEY)).all()
        from sqlalchemy import func  # noqa: PLC0415
        rows = {m.__tablename__: s.exec(select(func.count()).select_from(m)).one()[0]
                for m in (FinancialProfit, FinancialGrowth, FinancialBalance,
                          FinancialOperation, FinancialCashflow, FinancialDupont)}
    by_scope: dict[str, int] = {}
    for cp in cps:
        by_scope[cp.scope_key] = by_scope.get(cp.scope_key, 0) + 1
    _, used = _read_daily_count()
    return {"checkpoint": by_scope, "table_rows": rows, "today_used": used}
