"""质量因子 IC/分位收益研究脚本（v2 raw 因子 + 财务 provider 预载，防未来函数）。

验证三个质量 raw 因子（quality_roe / gross_margin / leverage）在成分股横截面上
对前向收益的预测力，并与价值对照（earnings_yield）对比互补性。

用法：
    python scripts/quality_factor_ic.py [--start 2016-01-01] [--end 2026-06-30] [--h 21]

输出：各因子 IC 统计（mean/IR/t-stat/正占比）、Q1-Q5 分位收益与多空价差、
     quality 因子与 earnings_yield 的横截面相关性。
"""
from __future__ import annotations

import argparse
import math
import statistics
from datetime import date, timedelta

from stockfu.backtest.engine import (
    _backtest_series_ctx,
    _preload_financial_reports,
    _preload_market_range,
)
from stockfu.backtest.factor_diag import (
    _forward_returns,
    _ic_by_date,
    _quantile_returns,
    _series_stats,
    _spearman,
)
from stockfu.factors.raw.earnings_yield import compute_earnings_yield
from stockfu.factors.raw.quality import (
    compute_gross_margin,
    compute_leverage,
    compute_quality_roe,
)

# 研究宇宙：中证500 + 沪深300 当前成分并集（历史成分偏差注明，与回测宇宙一致）
INDEXES = ("000905", "000300")
MIN_CROSS_SECTION = 200


def load_codes() -> list[str]:
    from sqlmodel import select
    from stockfu.db import session_scope
    from stockfu.models import IndexConstituent

    with session_scope() as s:
        rows = s.exec(select(IndexConstituent.asset_code).where(
            IndexConstituent.index_code.in_(INDEXES))).all()
    return sorted(set(rows))


def month_end_dates(sctx, start: date, end: date) -> list[date]:
    """预载日历中 [start, end] 的月末交易日。"""
    dates = sctx.dates
    out: list[date] = []
    for d in dates:
        if d < start or d > end:
            continue
        if out and out[-1].year == d.year and out[-1].month == d.month:
            out[-1] = d          # 同一月份替换为更晚的交易日
        else:
            out.append(d)
    return out


def build_panels(codes: list[str], signal_days: list[date], h: int, sctx):
    """各因子 raw 面板 + 前向收益面板（provider 在位零 DB）。"""
    computers = {
        "quality_roe": lambda c, t: compute_quality_roe(c, t),
        "gross_margin": lambda c, t: compute_gross_margin(c, t),
        "leverage": lambda c, t: compute_leverage(c, t),
        "earnings_yield": lambda c, t: compute_earnings_yield(c, t),
    }
    panels: dict[str, dict[tuple[str, date], float]] = {k: {} for k in computers}
    missing: dict[str, int] = {k: 0 for k in computers}
    total = 0
    for t in signal_days:
        for c in codes:
            total += 1
            for k, fn in computers.items():
                obs = fn(c, t)
                if obs.valid and obs.raw_value is not None:
                    panels[k][(c, t)] = obs.raw_value
                else:
                    missing[k] += 1
    # 前向收益：从预载行情列式结构直接构建 {code: {date: close}}
    series, dates, _date_idx, _valid = sctx
    price_panel: dict[str, dict[date, float]] = {}
    for c in codes:
        cols = series.get(c)
        if cols is None:
            continue
        px: dict[date, float] = {}
        arr = cols["c"]
        for i, d in enumerate(dates):
            v = arr[i]
            if not math.isnan(v):
                px[d] = float(v)
        price_panel[c] = px
    fwd = _forward_returns(price_panel, signal_days, list(dates), (h,))[h]
    for k in panels:
        missing[k] = round(missing[k] / max(total, 1) * 100, 1)
    return panels, fwd, missing


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--end", default="2026-06-30")
    ap.add_argument("--h", type=int, default=21, help="前向收益交易日数")
    args = ap.parse_args()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    codes = load_codes()
    print(f"宇宙: {len(codes)} 只（{' + '.join(INDEXES)} 当前成分并集，历史成分偏差注明）")
    pre_start = start - timedelta(days=400)
    sctx = _preload_market_range(codes, pre_start, end)
    fin = _preload_financial_reports(codes, end)
    signal_days = month_end_dates(sctx, start, end)
    print(f"采样日: {len(signal_days)} 个月末交易日（{signal_days[0]} ~ {signal_days[-1]}）")

    with _backtest_series_ctx(sctx, None, fin):
        panels, fwd, missing = build_panels(codes, signal_days, args.h, sctx)
        print(f"\n== 前向收益 h={args.h} 日 IC 统计 ==")
        print(f"{'因子':<16}{'IC均值%':>8}{'IC_std%':>8}{'IR':>8}{'t值':>8}{'正占比':>8}{'缺失率':>8}")
        for k, panel in panels.items():
            ics = _ic_by_date(panel, fwd, signal_days)
            st = _series_stats(ics)
            print(f"{k:<16}{st['mean_ic']*100:>8.2f}{st['ic_std']*100:>8.2f}"
                  f"{st['ic_ir']:>8.2f}{st['t_stat']:>8.2f}{st['pct_positive']:>7.1f}%"
                  f"{missing[k]:>7.1f}%")
        print("\n== Q1-Q5 分位收益（%·月）与多空价差 ==")
        print(f"{'因子':<16}{'Q1':>7}{'Q2':>7}{'Q3':>7}{'Q4':>7}{'Q5':>7}{'多空':>8}{'单调':>7}")
        for k, panel in panels.items():
            means, spread, mono, _m = _quantile_returns(panel, fwd, signal_days, 5)
            print(f"{k:<16}" + "".join(f"{x:>7.2f}" if x == x else f"{'nan':>7}" for x in means)
                  + f"{spread:>8.2f}{mono:>7.2f}")
        print("\n== 质量 × 价值（earnings_yield）横截面 Spearman 相关性 ==")
        for k in ("quality_roe", "gross_margin", "leverage"):
            corrs = []
            for t in signal_days:
                pairs = [(panels[k].get((c, t)), panels["earnings_yield"].get((c, t)))
                         for c in codes]
                pairs = [(x, y) for x, y in pairs if x is not None and y is not None]
                if len(pairs) > MIN_CROSS_SECTION:
                    corrs.append(_spearman([p[0] for p in pairs], [p[1] for p in pairs]))
            if corrs:
                print(f"  {k:<16} mean_rho={statistics.mean(corrs):+.3f}  "
                      f"min={min(corrs):+.2f} max={max(corrs):+.2f}  n={len(corrs)}")


if __name__ == "__main__":
    main()
