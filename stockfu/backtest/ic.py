"""横截面因子 IC 诊断的纯函数集（自 V1 factor_diag 迁出，scripts 复用）。

V1 因子诊断 CLI（--factor-diag）已随 V1 引擎移除；本模块保留研究脚本
（scripts/offense_factor_ic.py、quality_factor_ic.py 等）依赖的无状态
统计函数：Spearman 相关、逐日截面 IC、分桶收益与前向收益面板。
"""
from __future__ import annotations

import math
from datetime import date

MIN_CROSS_SECTION = 5                 # 单日参与 IC 的最少标的数(<此值该日不计)


def _rank_avg(values: list[float]) -> list[float]:
    """平均秩(并列取平均),返回与 values 同序的秩列表(1-based)。"""
    n = len(values)
    order = sorted(range(n), key=lambda i: (values[i], i))  # 值升序,i 兜底保稳定
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + 1 + j + 1) / 2.0          # 1-based 位 i+1..j+1 的平均
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return float("nan")
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = syy = sxy = 0.0
    for x, y in zip(xs, ys):
        dx, dy = x - mx, y - my
        sxx += dx * dx
        syy += dy * dy
        sxy += dx * dy
    if sxx <= 0 or syy <= 0:
        return float("nan")
    return sxy / (math.sqrt(sxx) * math.sqrt(syy))


def _spearman(xs: list[float], ys: list[float]) -> float:
    """Spearman = Pearson(rank(x), rank(y))。"""
    return _pearson(_rank_avg(xs), _rank_avg(ys))


def _series_stats(series: list[float]) -> dict:
    """日 IC 序列的聚合统计。"""
    n = len(series)
    if n == 0:
        return {"mean_ic": None, "ic_std": None, "ic_ir": None, "t_stat": None,
                "pct_positive": None, "n_days": 0}
    mean = sum(series) / n
    var = sum((x - mean) ** 2 for x in series) / (n - 1) if n > 1 else 0.0
    std = math.sqrt(var)
    ir = mean / std if std > 0 else None
    tstat = (mean / std) * math.sqrt(n) if std > 0 else None
    pos = sum(1 for x in series if x > 0) / n * 100
    return {"mean_ic": mean, "ic_std": std, "ic_ir": ir, "t_stat": tstat,
            "pct_positive": pos, "n_days": n}


def _forward_returns(price_panel: dict[str, dict[date, float]],
                     signal_days: list[date], cal_ext: list[date],
                     periods: tuple[int, ...]
                     ) -> dict[int, dict[tuple[str, date], float]]:
    """各前向周期 h → {(code, signal_date): price[t+h]÷price[t]−1}。

    用扩展交易日历 cal_ext 的位次往前推 h 个交易日(对齐"前向 h 日"语义,跨停牌不漂移:
    仅当 code 在 t 与 t+h 两天都有收盘价才计该观测)。
    """
    idx = {d: i for i, d in enumerate(cal_ext)}
    n = len(cal_ext)
    out: dict[int, dict[tuple[str, date], float]] = {h: {} for h in periods}
    for code, px in price_panel.items():
        for t, p0 in px.items():
            i = idx.get(t)
            if i is None:
                continue            # signal 日不在扩展日历(理论上不会)
            for h in periods:
                j = i + h
                if j >= n:
                    break
                p1 = px.get(cal_ext[j])
                if p1 is not None and p0 > 0:
                    out[h][(code, t)] = p1 / p0 - 1
    return out


def _ic_by_date(panel: dict[tuple[str, date], float],
                fwd: dict[tuple[str, date], float],
                signal_days: list[date]) -> list[float]:
    """逐日横截面 Spearman(factor, forward_return) → 日 IC 序列。"""
    by_date: dict[date, tuple[list[float], list[float]]] = {}
    for (code, t), f in panel.items():
        fr = fwd.get((code, t))
        if fr is None:
            continue
        slot = by_date.setdefault(t, ([], []))
        slot[0].append(f)
        slot[1].append(fr)
    series: list[float] = []
    for t in signal_days:
        slot = by_date.get(t)
        if not slot or len(slot[0]) < MIN_CROSS_SECTION:
            continue
        r = _spearman(slot[0], slot[1])
        if r == r:                  # 非 nan
            series.append(r)
    return series


def _quantile_returns(panel, fwd, signal_days, n_q):
    """按因子值横截面分 n_q 桶(Q1 最弱…QN 最强),返回 (各桶前向收益均值, 多空价差, 单调性)。

    单调性 = 桶号[1..n] 与各桶均值的 Spearman(1.0=完全单调;正=因子方向与前向收益同向)。
    同时返回 memberships {q: {date: frozenset[codes]}} 供换手计算。
    """
    by_date: dict[date, list[tuple[str, float, float]]] = {}  # (code, factor, fwd)
    for (code, t), f in panel.items():
        fr = fwd.get((code, t))
        if fr is None:
            continue
        by_date.setdefault(t, []).append((code, f, fr))

    sums = [0.0] * n_q
    cnts = [0] * n_q
    memberships: dict[int, dict[date, frozenset]] = {q: {} for q in range(n_q)}
    for t in signal_days:
        items = by_date.get(t)
        if not items or len(items) < n_q:
            continue
        items.sort(key=lambda x: (x[1], x[0]))          # factor 升序,code 兜底
        m = len(items)
        per_q: dict[int, list[str]] = {q: [] for q in range(n_q)}
        for k, (code, _f, fr) in enumerate(items):
            q = min(int(k / m * n_q), n_q - 1)          # 等量分桶
            sums[q] += fr
            cnts[q] += 1
            per_q[q].append(code)
        for q, cs in per_q.items():
            if cs:
                memberships[q][t] = frozenset(cs)

    means = [(sums[q] / cnts[q] * 100 if cnts[q] else float("nan")) for q in range(n_q)]  # → %
    spread = (means[-1] - means[0]) if all(c for c in cnts) else float("nan")
    valid = [x for x in means if x == x]                 # 非 nan
    mono = _spearman(list(range(1, n_q + 1)), means) if len(valid) == n_q else float("nan")
    return means, spread, mono, memberships
