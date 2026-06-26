"""每日定时任务：抓行情/分红/ETF份额 → 落库(天级快照) → 算指数。

触发方式：
  python main.py --fetch      # 立即跑一次 run_daily_job
  python main.py --backfill   # 回填关键标的 90 日历史（首次必跑，指数才能算）
  python main.py --schedule   # APScheduler 按 daily_cron 长驻运行
"""
from __future__ import annotations

from datetime import date

from sqlmodel import select

from stockfu.db import init_db, session_scope
from stockfu.models import (Asset, FundFlowSnapshot, QuoteSnapshot)

# 指数基准 + 资金流追踪标的：宽基 & 热门行业 ETF
INDEX_ETFS = [
    "510300",  # 沪深300
    "510500",  # 中证500
    "159915",  # 创业板ETF
    "512100",  # 中证1000
    "588000",  # 科创50
    "512480",  # 半导体
    "512690",  # 白酒
    "512010",  # 医药
    "515030",  # 新能源车
    "512800",  # 银行
]


def _upsert_quote(code: str) -> bool:
    from stockfu.data.manager import get_manager
    q = get_manager().get_quote(code)
    if not q:
        return False
    today = date.today()
    with session_scope() as s:
        snap = s.exec(select(QuoteSnapshot).where(
            QuoteSnapshot.asset_code == code, QuoteSnapshot.quote_date == today)).first()
        snap = snap or QuoteSnapshot(asset_code=code, quote_date=today)
        snap.open, snap.high, snap.low, snap.close = q.open, q.high, q.low, q.price
        snap.pct_chg, snap.volume, snap.amount = q.pct_chg, q.volume, q.amount
        snap.pe, snap.pb, snap.market_cap = q.pe, q.pb, q.market_cap
        if snap.id is None:
            s.add(snap)
        s.commit()
    return True


def _upsert_fundflow(code: str) -> bool:
    from stockfu.data.manager import get_manager
    ff = get_manager().get_etf_fund_flow(code)
    if not ff:
        return False
    today = date.today()
    with session_scope() as s:
        snap = s.exec(select(FundFlowSnapshot).where(
            FundFlowSnapshot.etf_code == code, FundFlowSnapshot.snap_date == today)).first()
        snap = snap or FundFlowSnapshot(etf_code=code, snap_date=today)
        snap.nav, snap.shares_outstanding = ff.get("nav"), ff.get("shares")
        snap.net_inflow = ff.get("amount")
        if snap.id is None:
            s.add(snap)
        s.commit()
    return True


def backfill_kline(code: str, days: int = 90) -> int:
    """拉历史日K灌入 quote_snapshot（首次/补数据）。返回新增条数。"""
    from stockfu.data.manager import get_manager
    bars = get_manager().get_kline(code, days)
    if not bars:
        return 0
    n = 0
    with session_scope() as s:
        have = {q.quote_date for q in s.exec(
            select(QuoteSnapshot).where(QuoteSnapshot.asset_code == code)).all()}
        for b in bars:
            if b.date in have:
                continue
            s.add(QuoteSnapshot(
                asset_code=code, quote_date=b.date, open=b.open, high=b.high,
                low=b.low, close=b.close, volume=b.volume, amount=b.amount,
            ))
            n += 1
        s.commit()
    return n


def run_backfill(days: int = 90) -> dict:
    """回填宽基/行业 ETF + 自选 的历史，供指数/情绪计算。"""
    init_db()
    with session_scope() as s:
        watch = [a.code for a in s.exec(select(Asset)).all()]
    targets = list(dict.fromkeys(INDEX_ETFS + watch))  # 去重保序
    result: dict[str, int] = {}
    for code in targets:
        try:
            result[code] = backfill_kline(code, days)
        except Exception as exc:  # noqa: BLE001
            result[code] = -1
    return result


def ensure_stock_data_and_index(code: str, days: int = 1825) -> dict:
    """单只个股：历史不足则补 K 线 + 抓今日行情 + 算个股三层情绪指数落库。

    供 TUI「加个股即算」与将来 CLI 复用；返回摘要 dict。
    """
    from stockfu.services.composite import compute_stock, save

    with session_scope() as s:
        have = len(s.exec(select(QuoteSnapshot).where(
            QuoteSnapshot.asset_code == code)).all())
    backfilled = backfill_kline(code, days) if have < 60 else 0  # 历史够就不重复拉
    quoted = _upsert_quote(code)
    result = compute_stock(code)
    save(result)
    return {
        "history_before": have,
        "backfilled": backfilled,
        "quoted": quoted,
        "fear": result.get("fear"),
        "greed": result.get("greed"),
        "heat": result.get("heat"),
    }


def run_daily_job() -> dict:
    """每日：行情落库 + 分红落库 + ETF份额落库 + 算指数。"""
    init_db()
    from stockfu.services import indices as indices_svc
    from stockfu.services import dividend as div_svc

    with session_scope() as s:
        codes = [a.code for a in s.exec(select(Asset)).all()]
    key_codes = list(dict.fromkeys(codes + INDEX_ETFS))

    quotes = sum(1 for c in key_codes if _upsert_quote(c))
    divs = sum(div_svc.persist_dividends(c) for c in codes)
    flows = sum(1 for c in INDEX_ETFS if _upsert_fundflow(c))
    indices = indices_svc.compute_and_save()
    # 三层情绪指数（市场 / 个股 / 板块）
    from stockfu.services import composite
    comp = composite.compute_all(codes)
    return {"quotes": quotes, "dividends": divs,
            "fundflow_etfs": flows, "indices": indices,
            "composite_levels": len(comp)}


def run_schedule() -> None:
    """APScheduler 长驻，按 settings.daily_cron 跑 run_daily_job。"""
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    from stockfu.config import settings

    init_db()
    parts = settings.daily_cron.split()
    trig = CronTrigger(minute=parts[0], hour=parts[1], day=parts[2],
                       month=parts[3], day_of_week=parts[4])
    sched = BlockingScheduler(timezone="Asia/Shanghai")
    sched.add_job(run_daily_job, trig, id="daily", max_instances=1, coalesce=True)
    print(f"调度已启动，cron={settings.daily_cron}（Asia/Shanghai）。Ctrl-C 退出。")
    sched.start()
