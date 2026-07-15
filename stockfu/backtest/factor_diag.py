"""因子诊断层(alphalens 思路):单算子连续 score → IC / 分位收益 / 换手 / 衰减。

验证单个因子不必搭整条策略管道(算子→策略→rebalancer→执行)。G10 后算子 score 直出
连续值(不 clamp),rebalancer 截面排名已用 total_score——本层把"单算子 score 在全市场
横截面上对前向收益的预测力"独立量化,补业界因子研究工作流缺口。

四件套(均纯 Python,与 engine._metrics 同款无 numpy/pandas 依赖):
  IC        : 每日横截面 Spearman(factor[t], forward_return[t]) → 序列统计
              (mean / std / IR=mean÷std / t-stat=mean÷std×√N / 正 IC 占比 / 天数)
  分位收益  : 按因子值横截面分 N 桶(Q1 最弱 … QN 最强),各桶前向收益均值 + 多空价差 + 单调性
  换手      : 各分位组合的日均成员变动率(对称差/并集),衡量因子组合的换手成本
  衰减      : IC 随前向周期(1/5/10/21 交易日)的变化 → 因子预测力的时间结构

防未来函数(红线):
  - 因子值在 as_of=t 算出,算子取数 quote_series/valuation_percentile 均 `<= as_of`(已防护)。
  - 前向收益 = price[t+h]÷price[t]−1 用 t *之后* 的价格——这正是被预测的对象,不是泄露。
  - IC 严格按日横截面算后再对日序列聚合(不跨日混池)。
  - 分位分桶/排名用平均秩 + code 兜底,确定性可复现。
"""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

from sqlmodel import select

from stockfu.db import session_scope
from stockfu.models import QuoteSnapshot

DEFAULT_PERIODS = (1, 5, 10, 21)      # 前向收益周期(交易日):1日/1周/2周/1月
DEFAULT_QUANTILES = 5
DEFAULT_PRIMARY_PERIOD = 5            # 分位收益/换手的主周期(衰减表覆盖全部周期)
MIN_CROSS_SECTION = 5                 # 单日参与 IC 的最少标的数(<此值该日不计)


# =====================================================================
# 纯 Python 统计原语
# =====================================================================


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


# =====================================================================
# 面板构建
# =====================================================================


def _build_factor_panel(operator_id: str, params: dict, codes: list[str],
                        signal_days: list[date], max_workers: int = 4,
                        progress: bool = False
                        ) -> tuple[dict[tuple[str, date], float], dict]:
    """单算子 score 面板 {(code, as_of): score} + 宇宙 meta。

    复用回测算子缓存(operator_result):逐日批量读命中 → 并发算 miss → 单日批量落库。
    每日先滤 U(t)(list_date/ST/停牌),再算因子——截面 IC 无次新名单污染。
    指纹走 single_operator_fingerprint(与回测同款),跨场景互通。
    """
    from stockfu.ai.operator_cache import (get_operator_results_batch,
                                           save_operator_results_day)
    from stockfu.ai.operators.base import OpContext
    from stockfu.ai.operators.registry import get_operator_class
    from stockfu.ai.operators.runner import single_operator_fingerprint

    cls = get_operator_class(operator_id)
    if cls is None:
        raise ValueError(f"未知算子 '{operator_id}'(不在注册表)")
    if cls.type != "math":
        raise ValueError(f"因子诊断只支持 math 算子('{operator_id}' 是 {cls.type})")
    fp = single_operator_fingerprint(operator_id, params)
    inst = cls()
    panel: dict[tuple[str, date], float] = {}

    # 时点宇宙:每日只对 U(t) 算因子,避免次新/ST/停牌污染截面 IC
    from stockfu.services.universe import UniverseContext, UniverseRules
    uni = UniverseContext.load(codes, UniverseRules())
    day_sizes: list[int] = []

    for i, as_of in enumerate(signal_days):
        from stockfu.services.universe import load_day_flags
        flags = load_day_flags(codes, as_of)
        day_codes = sorted(uni.eligible_on(as_of, flags))
        day_sizes.append(len(day_codes))
        if not day_codes:
            continue

        cached = get_operator_results_batch(day_codes, as_of, [(operator_id, fp)])
        misses = [c for c in day_codes if (c, operator_id) not in cached]

        computed: dict[str, object] = {}
        if misses:
            def _eval(c):   # 捕获当轮 as_of/inst/params(每轮重定义,无跨轮串扰)
                return inst.run(OpContext(code=c, name="", as_of=as_of), params)
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                fut = {pool.submit(_eval, c): c for c in misses}
                for f in as_completed(fut):
                    c = fut[f]
                    try:
                        computed[c] = f.result()
                    except Exception:  # noqa: BLE001
                        pass            # 单票失败不阻断(数据缺口),跳过该观测
            if computed:
                save_operator_results_day(computed, as_of, operator_id, fp, cls.type)

        for c in day_codes:
            r = cached.get((c, operator_id)) or computed.get(c)
            if r is not None and r.value is not None and r.score is not None:
                panel[(c, as_of)] = r.score

        if progress and (i % 20 == 0 or i == len(signal_days) - 1):
            print(f"  因子面板 {i + 1}/{len(signal_days)} 日 "
                  f"({as_of})  U={len(day_codes)} miss {len(misses)}  累计观测 {len(panel)}",
                  flush=True)
    return panel, uni.summary(day_sizes)


def _build_price_panel(codes: list[str], d_min: date, d_max: date
                       ) -> dict[str, dict[date, float]]:
    """{code: {date: close}} 一次 SELECT 取全(codes × 日期区间)。"""
    out: dict[str, dict[date, float]] = {}
    with session_scope() as s:
        rows = s.exec(select(QuoteSnapshot.asset_code, QuoteSnapshot.quote_date,
                             QuoteSnapshot.close).where(
            QuoteSnapshot.asset_code.in_(codes),
            QuoteSnapshot.quote_date >= d_min,
            QuoteSnapshot.quote_date <= d_max,
            QuoteSnapshot.close > 0,
        )).all()
    for code, d, close in rows:
        out.setdefault(code, {})[d] = float(close)
    return out


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


# =====================================================================
# 因子诊断核心
# =====================================================================


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


def _turnover(memberships: dict[int, dict[date, frozenset]],
              signal_days: list[date]) -> dict[int, float]:
    """各分位组合的日均成员变动率 = mean_t |S_t △ S_{t-1}| / |S_t ∪ S_{t-1}|。

    衡量持有该分位组合的换手成本(0=成员恒定,1=每日全换)。等权组合下约为组合换手率的一半。
    """
    out: dict[int, float] = {}
    for q, per_day in memberships.items():
        prev: frozenset | None = None
        diffs: list[float] = []
        for t in signal_days:
            cur = per_day.get(t)
            if cur is None:
                prev = None
                continue
            if prev:
                union = prev | cur
                if union:
                    diffs.append(len(prev ^ cur) / len(union))
            prev = cur
        out[q] = (sum(diffs) / len(diffs)) if diffs else float("nan")
    return out


# =====================================================================
# 主入口
# =====================================================================


def run_factor_diag(operator_id: str, params: dict | None, codes: list[str],
                    start, end, periods: tuple[int, ...] = DEFAULT_PERIODS,
                    n_quantiles: int = DEFAULT_QUANTILES,
                    primary_period: int = DEFAULT_PRIMARY_PERIOD,
                    max_workers: int = 4, progress: bool = False) -> dict:
    """跑单算子因子诊断,返回完整报告 dict(可 json 落盘 + CLI 打印)。

    codes: 诊断标的池(横截面 IC 需要足够多标的,建议 ≥50);start/end: 信号区间;
    periods: 前向收益周期(交易日);n_quantiles: 分位桶数;primary_period: 分位收益/
            换手主周期(衰减 IC 表覆盖全部 periods)。
    """
    from stockfu.backtest.engine import _trade_calendar_days

    if isinstance(start, str):
        start = date.fromisoformat(start)
    if isinstance(end, str):
        end = date.fromisoformat(end)
    max_h = max(periods)
    signal_days = _trade_calendar_days(start, end)
    # 扩展日历:前向收益需要 end 之后 max_h 个交易日(日历日缓冲 ≥ 2× 恒够)
    cal_ext = _trade_calendar_days(start, end + timedelta(days=max_h * 2 + 10))
    if not signal_days:
        raise ValueError(f"区间 {start}→{end} 无交易日(交易日历为空)")

    panel, uni_meta = _build_factor_panel(
        operator_id, params or {}, codes, signal_days,
        max_workers=max_workers, progress=progress)
    price_panel = _build_price_panel(codes, min(cal_ext), max(cal_ext))
    fwd = _forward_returns(price_panel, signal_days, cal_ext, periods)

    # IC(各周期)——衰减表
    ic = {h: _series_stats(_ic_by_date(panel, fwd[h], signal_days)) for h in periods}
    # 分位收益 + 换手(主周期)
    pperiod = primary_period if primary_period in fwd else (periods[0] if periods else 5)
    qmeans, qspread, qmono, memberships = _quantile_returns(
        panel, fwd.get(pperiod, {}), signal_days, n_quantiles)
    turnover = _turnover(memberships, signal_days)
    ls_turnover = ((turnover.get(n_quantiles - 1, float("nan"))
                    + turnover.get(0, float("nan"))) / 2) if turnover else float("nan")

    return {
        "operator": operator_id,
        "params": params or {},
        "start": start.isoformat(),
        "end": end.isoformat(),
        "universe_size": len(codes),
        "universe": uni_meta,
        "n_signal_days": len(signal_days),
        "factor_observations": len(panel),
        "periods": list(periods),
        "n_quantiles": n_quantiles,
        "primary_period": pperiod,
        "ic": {str(h): ic[h] for h in periods},
        "quantile_returns": [round(x, 4) if x == x else None for x in qmeans],
        "quantile_spread": round(qspread, 4) if qspread == qspread else None,
        "quantile_monotonicity": round(qmono, 3) if qmono == qmono else None,
        "turnover": {str(q): (round(v, 4) if v == v else None) for q, v in turnover.items()},
        "long_short_turnover": round(ls_turnover, 4) if ls_turnover == ls_turnover else None,
    }
