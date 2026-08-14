"""东财 datacenter-web 财务三表 PIT 回补（按报告期拉全市场，2026-08）。

替代 baostock 方案：baostock 按 (股票×年×季) 逐次调用约 82 万次、受 5 万/天上限
需 15-20 天；东财按报告期一次返回全市场（~5000 只），66 报告期 × 3 接口 ≈
2,400 次请求、约 1-2 小时完成。设计见 docs/SPECS/financial-data-design.md。

流程：
- 报告期列表：2010Q1 → 最近已结束报告期（动态按今天计算）。
- 每接口 × 每报告期：分页拉取（pageSize=500）→ 过滤 A 股（0/3/6 开头）→ 落库。
- checkpoint：task_key="financial-em-v1"，scope_key=接口，item_key=报告期；断点续传。
- 限流：分页间隔 0.3-0.5s；单页失败重试 2 次；报告期失败留 failed 下次续跑。
- 无每日配额（东财 datacenter-web 实测宽松；push2/push2his 仍封死，不涉及）。

用法（main.py 入口）：
  python3 main.py --backfill-financial                         # 全量 2010Q1 起
  python3 main.py --backfill-financial --fin-reports 20240331,20240630
  python3 main.py --backfill-financial --fin-status            # 进度统计
"""
from __future__ import annotations

import logging
import time
from datetime import date
from pathlib import Path

import requests
from sqlalchemy import func
from sqlmodel import select

from stockfu.db import session_scope
from stockfu.models import (BackfillCheckpoint, FinancialBalance, FinancialCashflow,
                            FinancialGrowth, FinancialProfit)

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]

TASK_KEY = "financial-em-v1"
API_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
PAGE_SIZE = 500
PAGE_SLEEP = (0.3, 0.5)          # 分页间隔（秒）
MAX_PAGE_RETRY = 2               # 单页失败重试次数
YEAR_FROM = 2010                 # 东财按报告期数据从 2010Q1 起

# A 股过滤（与回测池一致）：深主板 0 / 创业板 3 / 沪主板 6 开头
A_SHARE_PREFIXES = ("0", "3", "6")

# 资产负债表/现金流量表共用的过滤条件（排除北交所、只留 A 股类型）
BAL_FILTER = '(SECURITY_TYPE_CODE in ("058001001","058001008"))' \
             '(TRADE_MARKET_CODE!="069001017")(REPORT_DATE=\'%s\')'

# 接口定义：名称 → (reportName, 报告期过滤模板, [(模型, 源字段→模型字段映射), ...])
# 一个报告期的数据可回填多张表（如业绩报表的同比字段写 financial_growth）。
REPORT_INTERFACES: dict[str, dict] = {
    "profit": {
        "report_name": "RPT_LICO_FN_CPD",
        "filter": "(REPORTDATE='%s')",
        "targets": [
            (FinancialProfit, {
                "WEIGHTAVG_ROE": "roe_avg",
                "XSMLL": "gp_margin",
                "PARENT_NETPROFIT": "net_profit",
                "BASIC_EPS": "eps",
                "TOTAL_OPERATE_INCOME": "revenue",
                "YSTZ": "revenue_yoy",
                "SJLTZ": "net_profit_yoy",
                "BPS": "bps",
                "MGJYXJJE": "cash_per_share",
            }),
            (FinancialGrowth, {"SJLTZ": "yoy_ni"}),
        ],
    },
    "balance": {
        "report_name": "RPT_DMSK_FN_BALANCE",
        "filter": BAL_FILTER,
        "targets": [
            (FinancialBalance, {
                "TOTAL_ASSETS": "total_assets",
                "TOTAL_LIABILITIES": "total_liabilities",
                "DEBT_ASSET_RATIO": "liability_to_asset",
                "TOTAL_EQUITY": "equity",
                "MONETARYFUNDS": "monetary_fund",
                "ACCOUNTS_RECE": "receivables",
                "INVENTORY": "inventory",
                "ACCOUNTS_PAYABLE": "payable",
                "CURRENT_RATIO": "current_ratio",
            }),
        ],
    },
    "cashflow": {
        "report_name": "RPT_DMSK_FN_CASHFLOW",
        "filter": BAL_FILTER,
        "targets": [
            (FinancialCashflow, {
                "NETCASH_OPERATE": "net_cash_oper",
                "NETCASH_INVEST": "net_cash_inv",
                "NETCASH_FINANCE": "net_cash_fin",
                "CCE_ADD": "net_cash_total",
            }),
        ],
    },
}


def report_periods(year_from: int = YEAR_FROM, year_to: int | None = None) -> list[str]:
    """2010Q1 → 最近已结束报告期的全部季度末日期（YYYYMMDD）。"""
    yto = year_to or date.today().year
    today = date.today()
    periods: list[str] = []
    for year in range(year_from, yto + 1):
        for month, day in (("03", "31"), ("06", "30"), ("09", "30"), ("12", "31")):
            p = f"{year}{month}{day}"
            if date(year, int(month), int(day)) <= today:   # 未来报告期不拉
                periods.append(p)
    return periods


def _fetch_page(report_name: str, flt: str, page: int) -> tuple[list[dict], int]:
    """拉取一页，返回 (行列表, 总页数)；失败抛异常。"""
    params = {
        "sortColumns": "SECURITY_CODE", "sortTypes": "1",
        "pageSize": PAGE_SIZE, "pageNumber": page,
        "reportName": report_name, "columns": "ALL", "filter": flt,
    }
    r = requests.get(API_URL, params=params, timeout=30)
    d = r.json()
    result = d.get("result")
    if not result or not result.get("data"):
        raise RuntimeError(f"空响应: {d.get('message', 'no data')}")
    return result["data"], int(result.get("pages", 1))


def _checkpointed(scope_key: str, item_key: str) -> bool:
    with session_scope() as s:
        return s.exec(select(BackfillCheckpoint).where(
            BackfillCheckpoint.task_key == TASK_KEY,
            BackfillCheckpoint.scope_key == scope_key,
            BackfillCheckpoint.item_key == item_key)).first() is not None


def _mark_done(scope_key: str, item_key: str) -> None:
    with session_scope() as s:
        s.add(BackfillCheckpoint(task_key=TASK_KEY, scope_key=scope_key,
                                 item_key=item_key, status="success", attempts=0))
        s.commit()


def _upsert_row(model, code: str, report_date: str, notice_date: str | None,
                values: dict) -> None:
    """按 (asset_code, year, quarter) 唯一约束 upsert 一行。"""
    from datetime import date as date_cls  # noqa: PLC0415

    year = int(report_date[:4])
    quarter = {3: 1, 6: 2, 9: 3, 12: 4}[int(report_date[5:7])]
    with session_scope() as s:
        ex = s.exec(select(model).where(
            model.asset_code == code, model.year == year,
            model.quarter == quarter)).first()
        if ex is None:
            ex = model(asset_code=code, year=year, quarter=quarter)
            s.add(ex)
        for k, v in values.items():
            setattr(ex, k, v)
        ex.pub_date = date_cls.fromisoformat(notice_date) if notice_date else None
        ex.stat_date = date_cls.fromisoformat(report_date)
        s.commit()


def backfill_financial(interfaces: list[str] | None = None,
                       periods: list[str] | None = None) -> dict:
    """主回补。返回统计 dict。"""
    ifaces = interfaces or list(REPORT_INTERFACES)
    unknown = [i for i in ifaces if i not in REPORT_INTERFACES]
    if unknown:
        raise ValueError(f"未知接口: {unknown}；可选: {list(REPORT_INTERFACES)}")
    periods = periods or report_periods()
    log.info("计划 %d 个报告期 × %d 接口", len(periods), len(ifaces))

    stats: dict[str, dict[str, int]] = {
        i: {"done": 0, "empty": 0, "failed": 0, "skipped": 0} for i in ifaces
    }

    for iface in ifaces:
        conf = REPORT_INTERFACES[iface]
        for period in periods:
            item = period
            if _checkpointed(iface, item):
                stats[iface]["skipped"] += 1
                continue
            flt = conf["filter"] % f"{period[:4]}-{period[4:6]}-{period[6:]}"
            try:
                page, pages = 1, 1
                first = True
                while page <= pages:
                    data, pages = _fetch_page(conf["report_name"], flt, page)
                    for row in data:
                        code = str(row.get("SECURITY_CODE", ""))
                        if not code or code[0] not in A_SHARE_PREFIXES:
                            continue
                        notice = row.get("NOTICE_DATE")
                        if isinstance(notice, str):
                            notice = notice[:10]
                        for model, mapping in conf["targets"]:
                            vals = {m: _f(row[s]) for s, m in mapping.items()
                                    if row.get(s) not in (None, "")}
                            _upsert_row(model, code, f"{period[:4]}-{period[4:6]}-{period[6:]}",
                                        notice, vals)
                        first = False
                    if page < pages:
                        time.sleep(PAGE_SLEEP[0])
                    page += 1
                if first:
                    stats[iface]["empty"] += 1
                else:
                    stats[iface]["done"] += 1
                _mark_done(iface, item)
            except Exception:  # noqa: BLE001
                stats[iface]["failed"] += 1
                log.warning("报告期 %s 接口 %s 失败: %s", period, iface, _exc())
            time.sleep(PAGE_SLEEP[0])

    log.info("回补结束: %s", {k: v["done"] for k, v in stats.items()})
    return {"stats": stats}


def _exc() -> str:
    import traceback
    return traceback.format_exc(limit=1).strip().splitlines()[-1]


def _f(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def financial_status() -> dict:
    """只读进度统计。"""
    with session_scope() as s:
        cps = s.exec(select(BackfillCheckpoint).where(
            BackfillCheckpoint.task_key == TASK_KEY)).all()
        rows = {m.__tablename__: s.exec(select(func.count()).select_from(m)).one()[0]
                for m in (FinancialProfit, FinancialGrowth, FinancialBalance,
                          FinancialCashflow)}
    by_scope: dict[str, int] = {}
    for cp in cps:
        by_scope[cp.scope_key] = by_scope.get(cp.scope_key, 0) + 1
    return {"checkpoint": by_scope, "table_rows": rows}
