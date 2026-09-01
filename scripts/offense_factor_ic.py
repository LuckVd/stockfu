"""进攻策略因子诊断脚本（v2 raw 因子 + 财务 provider 预载，防未来函数）。

分析 momentum_growth_offense_v2 的四个进攻轴（动量 / 趋势线性度 / 净利同比成长 /
高弹性波动率）在成分股横截面上的前向收益预测力，并与防守对照（价值 E/P、低波）
对比相关性与互补性，判断哪根轴在进攻策略中拖后腿或贡献超额。

用法：
    python scripts/offense_factor_ic.py [--start 2016-01-01] [--end 2026-08-12] [--h 21]

输出：进攻/对照因子 IC 统计（mean/IR/t-stat/正占比）、Q1-Q5 分位收益与多空价差、
     各因子与 momentum 的横截面相关性。
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
from stockfu.backtest.ic import (
    _forward_returns,
    _ic_by_date,
    _quantile_returns,
    _series_stats,
    _spearman,
)
from stockfu.factors.raw.growth import compute_growth_accel, compute_growth_ni
from stockfu.factors.raw.momentum import compute_momentum
from stockfu.factors.raw.trend_linearity import compute_trend_linearity
from stockfu.factors.raw.volatility import compute_low_volatility_20d

INDEXES = ("000905", "000300")
MIN_CROSS_SECTION = 200
# 进攻轴 raw 计算器。high_vol 用低波实际值（进攻方向=反向，需反号看）。
COMPUTERS = {
    "momentum_12_1": lambda c, t: compute_momentum(c, t),
    "trend_linearity": lambda c, t: compute_trend_linearity(c, t),
    "growth_ni": lambda c, t: compute_growth_ni(c, t),
    "growth_accel": lambda c, t: compute_growth_accel(c, t),
    "low_vol(实际)": lambda c, t: compute_low_volatility_20d(c, t),
}

# 活跃度进攻轴：直接从预载行情列式数组读取（turn=换手率%, amt=成交额元），
# 无需 raw 计算器。断言：换手/成交额高 = 活跃，进攻候选，IC 为正才值得做。
ACTIVITY_KEYS = {
    "turnover(换手%)": "turn",
    "amount(成交额)": "amt",
}


def load_codes() -> list[str]:
    from sqlmodel import select
    from stockfu.db import session_scope
    from stockfu.models import IndexConstituent

    with session_scope() as s:
        rows = s.exec(select(IndexConstituent.asset_code).where(
            IndexConstituent.index_code.in_(INDEXES))).all()
    return sorted(set(rows))


def month_end_dates(sctx, start: date, end: date) -> list[date]:
    dates = sctx.dates
    out: list[date] = []
    for d in dates:
        if d < start or d > end:
            continue
        if out and out[-1].year == d.year and out[-1].month == d.month:
            out[-1] = d
        else:
            out.append(d)
    return out


def build_panels(codes: list[str], signal_days: list[date], h: int, sctx):
    computers = COMPUTERS
    panels: dict[str, dict[tuple[str, date], float]] = {k: {} for k in computers}
    missing: dict[str, int] = {k: 0 for k in computers}
    total = 0
    series, dates, _date_idx, _valid = sctx
    # 活跃度轴：按日从列式数组取 nonzero 真实值；成交额取对数(右偏压缩)
    for ak, col_key in ACTIVITY_KEYS.items():
        panels[ak] = {}
        missing[ak] = 0
    for t in signal_days:
        ti = _date_idx.get(t)
        for c in codes:
            total += 1
            for k, fn in computers.items():
                obs = fn(c, t)
                if obs.valid and obs.raw_value is not None:
                    panels[k][(c, t)] = obs.raw_value
                else:
                    missing[k] += 1
            cols = series.get(c)
            if cols is None or ti is None:
                continue
            for ak, col_key in ACTIVITY_KEYS.items():
                v = cols[col_key][ti]
                if not math.isnan(v) and v > 0:
                    panels[ak][(c, t)] = math.log(v) if col_key == "amt" else float(v)
                else:
                    missing[ak] += 1
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
    ap.add_argument("--end", default="2026-08-12")
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
        print(f"{'因子':<18}{'IC均值%':>8}{'IC_std%':>8}{'IR':>8}{'t值':>8}{'正占比':>8}{'缺失率':>8}")
        for k, panel in panels.items():
            ics = _ic_by_date(panel, fwd, signal_days)
            st = _series_stats(ics)
            print(f"{k:<18}{st['mean_ic']*100:>8.2f}{st['ic_std']*100:>8.2f}"
                  f"{st['ic_ir']:>8.2f}{st['t_stat']:>8.2f}{st['pct_positive']:>7.1f}%"
                  f"{missing[k]:>7.1f}%")
        print("\n== Q1-Q5 分位收益（%·月）与多空价差（low_vol为实际值，进攻方向是反号） ==")
        print(f"{'因子':<18}{'Q1(低)':>8}{'Q2':>7}{'Q3':>7}{'Q4':>7}{'Q5(高)':>7}{'多空':>8}{'单调':>7}")
        for k, panel in panels.items():
            means, spread, mono, _m = _quantile_returns(panel, fwd, signal_days, 5)
            print(f"{k:<18}" + "".join(f"{x:>7.2f}" if x == x else f"{'nan':>7}" for x in means)
                  + f"{spread:>8.2f}{mono:>7.2f}")
        print("\n== 各轴与 momentum（主攻）横截面 Spearman 相关性 ==")
        all_keys = list(COMPUTERS.keys()) + list(ACTIVITY_KEYS.keys())
        for k in all_keys:
            if k == "momentum_12_1":
                continue
            corrs = []
            for t in signal_days:
                pairs = [(panels[k].get((c, t)), panels["momentum_12_1"].get((c, t)))
                         for c in codes]
                pairs = [(x, y) for x, y in pairs if x is not None and y is not None]
                if len(pairs) > MIN_CROSS_SECTION:
                    corrs.append(_spearman([p[0] for p in pairs], [p[1] for p in pairs]))
            if corrs:
                print(f"  {k:<18} vs momentum: mean_rho={statistics.mean(corrs):+.3f}  "
                      f"n={len(corrs)}")


if __name__ == "__main__":
    main()
