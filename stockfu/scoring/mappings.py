"""因子评分映射算法。

严格对应 docs/SPECS/factor-strategy-score-v2.md §6。

三种原语:
- fixed_score (§6.1):固定锚点等比例线性插值,越界截断到端点分数。
- ecdf_score (§6.2):过去样本中秩经验分位,L+0.5E 中秩,与输入顺序无关。
- combine_hybrid (§6.3):绝对锚点 + 多历史分量加权,缺失只向 50 收缩、不冒充中性。

不变量:
- 所有分量已统一为「越高越好」的 0–100(knots 编码方向;ECDF 按 direction 翻转)。
- raw_value 缺失 → 整因子 score=50、coverage=0、maturity=immature(§6.3)。
- 最终 score 截断到闭区间 [0,100],且必为有限数。
"""
from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass

from stockfu.scoring.contracts import (
    Direction,
    Maturity,
    MissingReason,
)


Knots = list[tuple[float, float]]   # [(raw_value, score), ...] 按 raw 升序


def clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


# ------------------------------------------------------------------- §6.1 fixed


def fixed_score(raw_value: float | None, knots: Knots) -> float | None:
    """固定锚点等比例映射。

    相邻锚点间严格线性插值;低于最小锚点或高于最大锚点 → 端点分数(§6.1)。
    raw 缺失/NaN 或无锚点 → None。knots 必须按 raw 升序(由 profiles 校验)。
    """
    if raw_value is None or not knots:
        return None
    if isinstance(raw_value, float) and math.isnan(raw_value):
        return None
    xs = [k[0] for k in knots]
    ss = [k[1] for k in knots]
    if raw_value <= xs[0]:
        return float(ss[0])
    if raw_value >= xs[-1]:
        return float(ss[-1])
    i = bisect_right(xs, raw_value) - 1      # xs[i] <= raw_value < xs[i+1]
    if i < 0:
        i = 0
    x0, x1 = xs[i], xs[i + 1]
    s0, s1 = ss[i], ss[i + 1]
    if x1 == x0:
        return float(s0)
    t = (raw_value - x0) / (x1 - x0)
    return float(s0 + t * (s1 - s0))


# ------------------------------------------------------------------- §6.2 ECDF


@dataclass
class HistoryComponent:
    """单个历史分量的映射结果。"""

    score: float | None        # 0–100(越高越好),无样本/缺失 → None
    n: int                     # 实际样本数
    maturity_coef: float       # min(1, n/min_observations);无样本 → 0


def ecdf_score(raw_value: float | None, samples: list[float],
               direction: Direction, min_observations: int) -> HistoryComponent:
    """过去样本中秩经验分位(§6.2)。

    samples 只含 t-1 及以前的合格观测,**不得**含当日 x(由调用方保证)。
    中秩 percentile = (L + 0.5·E) / N,L=小于 x 的数量,E=等于 x 的数量。
    higher_is_better → 100·pct;lower_is_better → 100·(1-pct)。
    与资产输入顺序无关(基于排序 + 计数)。
    """
    s = [x for x in samples
         if x is not None and not (isinstance(x, float) and math.isnan(x))]
    n = len(s)
    if raw_value is None or n == 0:
        return HistoryComponent(None, n, 0.0)
    if isinstance(raw_value, float) and math.isnan(raw_value):
        return HistoryComponent(None, n, 0.0)
    s.sort()
    left = bisect_left(s, raw_value)         # < raw_value
    le = bisect_right(s, raw_value)          # <= raw_value
    equal = le - left
    pct = (left + 0.5 * equal) / n
    score = 100.0 * pct if direction == Direction.HIGHER_IS_BETTER else 100.0 * (1.0 - pct)
    score = clamp(score)
    if min_observations > 0:
        m = min(1.0, n / min_observations)
    else:
        m = 1.0 if n > 0 else 0.0
    return HistoryComponent(score, n, m)


def ecdf_score_sorted(raw_value: float | None, sorted_samples: list[float],
                      direction: Direction, min_observations: int) -> HistoryComponent:
    """同 ecdf_score 但 samples 已升序(用 bisect,不重 sort)。

    供共享的大池分量(market/industry)使用:同一 cutoff 下全市场共用一份已排序
    样本,避免逐股票重复 sort(性能关键,见 v2-notes)。
    """
    n = len(sorted_samples)
    if raw_value is None or n == 0:
        return HistoryComponent(None, n, 0.0)
    if isinstance(raw_value, float) and math.isnan(raw_value):
        return HistoryComponent(None, n, 0.0)
    left = bisect_left(sorted_samples, raw_value)
    le = bisect_right(sorted_samples, raw_value)
    equal = le - left
    pct = (left + 0.5 * equal) / n
    score = 100.0 * pct if direction == Direction.HIGHER_IS_BETTER else 100.0 * (1.0 - pct)
    score = clamp(score)
    if min_observations > 0:
        m = min(1.0, n / min_observations)
    else:
        m = 1.0 if n > 0 else 0.0
    return HistoryComponent(score, n, m)


# ------------------------------------------------------------------- §6.3 hybrid


@dataclass
class MappingResult:
    """一次完整映射的中间结果(供 FactorScoreObservation 填充)。"""

    score: float
    evidence_coverage: float
    maturity: Maturity
    absolute_score: float | None = None
    market_history_score: float | None = None
    industry_history_score: float | None = None
    self_history_score: float | None = None
    history_n: dict[str, int] = None  # type: ignore[assignment]
    warnings: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.history_n is None:
            self.history_n = {}
        if self.warnings is None:
            self.warnings = []


def combine_hybrid(
    raw_value: float | None,
    *,
    direction: Direction,
    absolute_knots: Knots | None,
    weights: dict[str, float],
    components: dict[str, HistoryComponent],
    missing_reason: MissingReason | None = None,
) -> MappingResult:
    """混合映射(§6.3)。

    weights: {absolute, market_history, industry_history, self_history} → 权重,和=1。
    components: {market_history, industry_history, self_history} → HistoryComponent。
    absolute_knots: absolute 分量的锚点(weights['absolute']>0 时必给)。

    score = 50 + Σ_j w_j·m_j·(component_score_j − 50)
    evidence_coverage = Σ_j w_j·m_j
    raw 缺失 → score=50, coverage=0, immature。最终截断到 [0,100]。
    """
    history_n = {name: comp.n for name, comp in components.items()}
    warnings: list[str] = []

    abs_w = weights.get("absolute", 0.0)
    abs_score = fixed_score(raw_value, absolute_knots) if (abs_w > 0 and absolute_knots) else None

    # raw 缺失或绝对分量不可得 → 整因子无证据(§6.3)。
    if raw_value is None or (isinstance(raw_value, float) and math.isnan(raw_value)) \
            or (abs_w > 0 and abs_score is None):
        return MappingResult(
            score=50.0, evidence_coverage=0.0, maturity=Maturity.IMMATURE,
            absolute_score=abs_score, history_n=history_n, warnings=warnings,
        )

    parts: list[tuple[float, float, float]] = []   # (weight, maturity_coef, component_score)
    if abs_w > 0 and abs_score is not None:
        parts.append((abs_w, 1.0, abs_score))      # absolute 恒成熟

    comp_scores: dict[str, float | None] = {
        "market_history": None, "industry_history": None, "self_history": None,
    }
    for name in ("market_history", "industry_history", "self_history"):
        w = weights.get(name, 0.0)
        comp = components.get(name)
        if w <= 0.0 or comp is None or comp.score is None:
            if w > 0.0 and (comp is None or comp.n == 0):
                warnings.append(f"{name} 无样本,收缩到 50")
            continue
        parts.append((w, comp.maturity_coef, comp.score))
        comp_scores[name] = comp.score
        if comp.maturity_coef < 1.0:
            warnings.append(f"{name} 未成熟(n={comp.n},m={comp.maturity_coef:.3f})")

    if not parts:
        # absolute 权重为 0 且无任何历史分量 → 无证据
        return MappingResult(
            score=50.0, evidence_coverage=0.0, maturity=Maturity.IMMATURE,
            history_n=history_n, warnings=warnings,
        )

    score = 50.0 + sum(w * m * (c - 50.0) for w, m, c in parts)
    coverage = sum(w * m for w, m, _c in parts)
    score = clamp(score)

    # 成熟度:有历史分量配置时,全部 mature 才 mature,否则 partial。
    hist_ws = [weights.get(n, 0.0) for n in
               ("market_history", "industry_history", "self_history")]
    has_history = any(w > 0 for w in hist_ws)
    if not has_history:
        maturity = Maturity.MATURE
    else:
        all_mature = all(
            (weights.get(n, 0.0) <= 0.0) or
            (components.get(n) is not None and components[n].maturity_coef >= 1.0)
            for n in ("market_history", "industry_history", "self_history")
        )
        maturity = Maturity.MATURE if all_mature else Maturity.PARTIAL

    return MappingResult(
        score=score, evidence_coverage=coverage, maturity=maturity,
        absolute_score=abs_score,
        market_history_score=comp_scores["market_history"],
        industry_history_score=comp_scores["industry_history"],
        self_history_score=comp_scores["self_history"],
        history_n=history_n, warnings=warnings,
    )
