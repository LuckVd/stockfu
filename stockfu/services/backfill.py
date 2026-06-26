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
from stockfu.models import DividendEvent, FactorSnapshot, QuoteSnapshot
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
