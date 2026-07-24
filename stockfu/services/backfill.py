"""历史数据回补（限速 + 断点续传）。

- backfill_margin_total()：两融总量历史序列（接口一次拉十几年，快）
- backfill_limit_up(days)：连板/涨停按天回补（限速1次/秒+断点续传）—— 低频慢任务
- backfill_margin_stock_recent(codes, days=10)：个股两融近 N 天
- compute_dividend_yield_series(code)：本地算股息率历史序列（用分红事件+价格，不花钱）
"""
from __future__ import annotations

import time
from datetime import date, timedelta

import pandas as pd
from sqlmodel import select

from stockfu.db import session_scope
from stockfu.models import (DividendEvent, FactorSnapshot, QuoteSnapshot,
                            SectorFlowSnapshot, SectorSnapshot)
from stockfu.services.market_data import _call, _f, _pick, limit_up_at


def _save_factor(level, scope, factor, d, raw) -> bool:
    if raw is None:
        return False
    with session_scope() as s:
        ex = s.exec(select(FactorSnapshot).where(
            FactorSnapshot.level == level, FactorSnapshot.scope == scope,
            FactorSnapshot.factor == factor, FactorSnapshot.snap_date == d)).first()
        fs = ex or FactorSnapshot(level=level, scope=scope, factor=factor, snap_date=d)
        fs.raw_value = float(raw)
        if fs.id is None:
            s.add(fs)
        s.commit()
    return True


def _has_factor(level, scope, factor, d) -> bool:
    with session_scope() as s:
        return s.exec(select(FactorSnapshot).where(
            FactorSnapshot.level == level, FactorSnapshot.scope == scope,
            FactorSnapshot.factor == factor, FactorSnapshot.snap_date == d)).first() is not None


def backfill_margin_total() -> int:
    """两融总量历史序列（stock_margin_sse 一次返回 ~2000 条历史）。"""
    df = _call([("stock_margin_sse", {}), ("stock_margin_szse", {})])
    if df is None:
        return 0
    date_col = next((c for c in df.columns if "日期" in str(c)), None)
    bal_col = (next((c for c in df.columns if "融资融券余额" in str(c)), None)
               or next((c for c in df.columns if "余额" in str(c)), None))
    if not date_col or not bal_col:
        return 0
    n = 0
    for _, r in df.iterrows():
        raw = r[date_col]
        try:
            d = pd.to_datetime(str(raw), format="%Y%m%d").date()
        except Exception:  # noqa: BLE001
            try:
                d = pd.to_datetime(raw).date()
            except Exception:  # noqa: BLE001
                continue
        if _save_factor("market", "MARKET", "margin_balance", d, _f(r[bal_col])):
            n += 1
    return n


def backfill_limit_up(days: int = 365, sleep: float = 2.0, retries: int = 2) -> dict:
    """连板/涨停按天回补（限速 + 重试 + 断点续传 + 连续失败中止）。

    东财对连续批量请求限流严重，建议低频后台小批量跑，靠断点续传分多次补齐。
    """
    today = date.today()
    filled = skipped = failed = 0
    consec_fail = 0
    for i in range(1, days + 1):
        d = today - timedelta(days=i)
        if _has_factor("market", "MARKET", "limit_chain", d):
            skipped += 1
            continue
        ok = False
        for _ in range(retries + 1):
            try:
                res = limit_up_at(d) or {}
                hc, lc = res.get("highest_chain"), res.get("limit_up_count")
                if hc is not None or lc is not None:
                    _save_factor("market", "MARKET", "limit_chain", d, hc)
                    _save_factor("market", "MARKET", "limit_count", d, lc)
                    filled += 1
                    ok = True
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(sleep)
        if not ok:
            failed += 1
            consec_fail += 1
            if consec_fail >= 10:
                print(f"  连续10次失败(疑似限流)，中止。已补 {filled} 天，重跑可断点续传")
                break
        else:
            consec_fail = 0
        time.sleep(sleep)
    return {"filled": filled, "skipped": skipped, "failed": failed, "days": days,
            "stopped_early": consec_fail >= 10}


def backfill_margin_stock_recent(codes, days: int = 10) -> dict:
    """个股两融近 N 天（每日拉全市场，筛自选）。"""
    import akshare as ak

    today = date.today()
    out: dict[str, int] = {}
    for i in range(days):
        d = today - timedelta(days=i)
        ds = d.strftime("%Y%m%d")
        df = None
        for fn in ("stock_margin_detail_sse", "stock_margin_detail_szse"):
            f = getattr(ak, fn, None)
            if not f:
                continue
            try:
                t = f(date=ds)
                if t is not None and not t.empty:
                    df = pd.concat([df, t]) if df is not None else t
            except Exception:  # noqa: BLE001
                continue
        if df is None:
            continue
        code_col = next((c for c in df.columns if "代码" in str(c)), None)
        if code_col is None:
            continue
        for code in codes:
            r = df[df[code_col].astype(str).str.contains(code)]
            if len(r):
                _save_factor("stock", code, "margin_balance", d, _f(_pick(r.iloc[0], "融资余额")))
        out[ds] = len(df)
    return out


def compute_dividend_yield_series(code) -> int:
    """本地算股息率历史序列：TTM(近365天每股分红) ÷ 当日close ×100。依赖价格历史已 backfill。"""
    with session_scope() as s:
        closes = s.exec(select(QuoteSnapshot).where(
            QuoteSnapshot.asset_code == code).order_by(QuoteSnapshot.quote_date)).all()
        divs = s.exec(select(DividendEvent).where(
            DividendEvent.asset_code == code)).all()
    divs_sorted = sorted([(d.ex_date, d.per_share_cash)
                          for d in divs if d.ex_date and d.per_share_cash])
    n = 0
    for snap in closes:
        if not snap.close or snap.close <= 0:
            continue
        lo = snap.quote_date - timedelta(days=365)
        ttm = sum(cash for ex, cash in divs_sorted if lo <= ex <= snap.quote_date)
        if ttm <= 0:
            continue
        _save_factor("stock", code, "dividend_yield", snap.quote_date,
                     round(ttm / snap.close * 100, 4))
        n += 1
    return n


# ---------- 板块资金流（同花顺源，绕开东财 push2/push2his 限流）----------
def backfill_sector_kline(sector_name: str, days: int = 1460) -> int:
    """拉行业板块指数历史K线灌入 sector_snapshot（同花顺，~4年逐日）。返回新增条数。

    范式同 backfill_kline：sorted → 集合去重 → 循环 add → commit。
    """
    from stockfu.data.manager import get_manager
    from stockfu.services.composite import SECTOR_THS_NAME
    ths = SECTOR_THS_NAME.get(sector_name) or sector_name   # 业务名 → 同花顺行业名(symbol)
    bars = sorted(get_manager().get_sector_kline(ths, days), key=lambda b: b.date)
    if not bars:
        return 0
    n = 0
    with session_scope() as s:
        have = {x.snap_date for x in s.exec(
            select(SectorSnapshot).where(SectorSnapshot.sector_name == sector_name)).all()}
        prev_close = None
        for b in bars:
            if b.date not in have:
                pct = round((b.close / prev_close - 1) * 100, 2) if prev_close else None
                s.add(SectorSnapshot(
                    sector_name=sector_name, snap_date=b.date, open=b.open, high=b.high,
                    low=b.low, close=b.close, pct_chg=pct, volume=b.volume, amount=b.amount,
                ))
                n += 1
            prev_close = b.close
        s.commit()
    return n


def backfill_market_fund_flow() -> int:
    """大盘资金流历史（主力/超大/大/中/小单净额+占比）灌入 factor_snapshot（level=market）。

    stock_market_fund_flow 一次返回 ~6 个月全量序列；每列一个 factor，scope=MARKET。
    复用 _save_factor upsert。失败（限流/空）返回 0。
    """
    from stockfu.data.manager import get_manager
    rows = get_manager().get_market_fund_flow()
    if not rows:
        return 0
    factors = (
        ("main_net_inflow", "main_net"), ("main_net_inflow_pct", "main_pct"),
        ("super_large_net", "super_net"), ("super_large_pct", "super_pct"),
        ("large_net", "large_net"), ("large_pct", "large_pct"),
        ("mid_net", "mid_net"), ("mid_pct", "mid_pct"),
        ("small_net", "small_net"), ("small_pct", "small_pct"),
    )
    n = 0
    for r in rows:
        d = r.get("date")
        if d is None:
            continue
        for factor, key in factors:
            if _save_factor("market", "MARKET", factor, d, r.get(key)):
                n += 1
    return n


def backfill_sector_flow(snap_date) -> int:
    """行业板块主力资金流落库 sector_flow_snapshot（每日 --fetch 攒历史）。

    snap_date: 目标交易日(已校验)，盖章用——不再用 date.today()（凌晨防错标）。
    东财 push2his 历史源限流不稳，故即时快照靠每日累积（首日无分位，越跑越准）。
    仅匹配 SECTOR_THS_NAME 里有映射的板块（精确匹配，避免「医药商业」误中「医药」）。
    返回写入行数。
    """
    from stockfu.data.manager import get_manager
    from stockfu.services.composite import SECTOR_THS_NAME
    from stockfu.services.quote_writer import _coerce_date

    d = _coerce_date(snap_date)
    flows = get_manager().get_sector_flow_today()
    if not flows:
        return 0
    want = {ths: name for name, ths in SECTOR_THS_NAME.items() if ths}  # ths行业名 → SECTOR_MAP键
    n = 0
    with session_scope() as s:
        for f in flows:
            sector = want.get(f.get("name", ""))
            if not sector:
                continue
            snap = s.exec(select(SectorFlowSnapshot).where(
                SectorFlowSnapshot.sector_name == sector,
                SectorFlowSnapshot.snap_date == d)).first()
            snap = snap or SectorFlowSnapshot(sector_name=sector, snap_date=d)
            snap.net_inflow = f.get("net_inflow")
            # 同花顺即时表无净占比；保留为空，历史东财源会补齐。
            snap.inflow = f.get("inflow")
            snap.outflow = f.get("outflow")
            snap.company_count = f.get("company_count")
            snap.leading_stock = f.get("leading_stock") or ""
            snap.leading_chg = f.get("leading_chg")
            snap.index_pct_chg = f.get("index_pct_chg")
            if snap.id is None:
                s.add(snap)
            n += 1
        s.commit()
    return n


def backfill_sector_flow_history(sector_names: list[str], *, pause_sec: float = 1.2) -> dict:
    """串行回补行业历史资金流，返回逐行业结果。

    东财历史接口按行业查询且会主动限流。本函数刻意不使用线程/协程：一次只发
    一个请求，每次请求后等待至少 ``pause_sec``。单行业失败只记录失败，不阻断
    其余行业；已有日期幂等跳过。调用者应把返回摘要留在日志，不能把失败行业
    当作当天资金流有效。
    """
    import time

    from stockfu.data.manager import get_manager

    out = {"requested": len(sector_names), "rows": 0, "ok": [], "failed": []}
    for i, name in enumerate(sector_names):
        rows = get_manager().get_sector_flow_history(name)
        if not rows:
            out["failed"].append(name)
        else:
            added = 0
            with session_scope() as s:
                existing = {r.snap_date for r in s.exec(select(SectorFlowSnapshot).where(
                    SectorFlowSnapshot.sector_name == name)).all()}
                for r in rows:
                    d = r.get("date")
                    if d is None or d in existing:
                        continue
                    s.add(SectorFlowSnapshot(
                        sector_name=name, snap_date=d, net_inflow=r.get("net_inflow"),
                        net_inflow_pct=r.get("net_inflow_pct"),
                    ))
                    added += 1
                s.commit()
            out["rows"] += added
            out["ok"].append({"name": name, "rows": len(rows), "added": added})
        if i + 1 < len(sector_names):
            time.sleep(max(1.0, pause_sec))
    return out


def backfill_sector_pulse_history(*, pause_sec: float = 1.2) -> dict:
    """初始化完整东方财富行业全景，严格串行、每行业最多两次请求。

    选择东方财富行业分类，是因为历史行情与历史资金流可按完全相同的名称取得；
    不与同花顺行业名交叉映射，避免卡片把不同口径拼成一行。
    """
    import time

    from stockfu.data.manager import get_manager

    manager = get_manager()
    names = manager.get_sector_names_em()
    result = {"requested": len(names), "quotes": 0, "flows": 0, "ok": [], "failed": []}
    for i, name in enumerate(names):
        bars = manager.get_sector_kline_em(name)
        if bars:
            # 复用已有 upsert 逻辑，但不再走不同分类的同花顺接口。
            with session_scope() as s:
                have = {x.snap_date for x in s.exec(select(SectorSnapshot).where(
                    SectorSnapshot.sector_name == name)).all()}
                prev = None
                for b in sorted(bars, key=lambda x: x.date):
                    if b.date not in have:
                        s.add(SectorSnapshot(sector_name=name, snap_date=b.date, open=b.open,
                            high=b.high, low=b.low, close=b.close,
                            pct_chg=round((b.close / prev - 1) * 100, 2) if prev else None,
                            volume=b.volume, amount=b.amount))
                        result["quotes"] += 1
                    prev = b.close
                s.commit()
        flows = manager.get_sector_flow_history(name)
        if flows:
            with session_scope() as s:
                have = {x.snap_date for x in s.exec(select(SectorFlowSnapshot).where(
                    SectorFlowSnapshot.sector_name == name)).all()}
                for r in flows:
                    if r["date"] not in have:
                        s.add(SectorFlowSnapshot(sector_name=name, snap_date=r["date"],
                            net_inflow=r.get("net_inflow"), net_inflow_pct=r.get("net_inflow_pct")))
                        result["flows"] += 1
                s.commit()
        if bars and flows:
            result["ok"].append(name)
        else:
            result["failed"].append(name)
        if i + 1 < len(names):
            time.sleep(max(1.0, pause_sec))
    return result
