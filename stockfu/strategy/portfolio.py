"""组合政策层(设计 §13.1、§19)。

只决定「如何持有」:从 strategy_score 选股 + 定目标权重,不修改分数、不做风险。
解耦关键:同一 alpha 换不同 portfolio_policy,strategy_score 必须完全相同(§4 不变量)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from stockfu.scoring.contracts import ScoreStatus, StrategyScoreObservation, fingerprint


@dataclass(frozen=True)
class SelectionSpec:
    method: str                       # top_n_above_score
    n: int
    minimum_score: float


@dataclass(frozen=True)
class PortfolioPolicy:
    portfolio_policy_id: str
    version: int
    rebalance: str                    # weekly | monthly | daily
    selection: SelectionSpec
    weighting: str                    # equal
    max_single_weight: float
    max_gross: float
    min_amount_20d: float
    minimum_listing_days: int
    max_industry_weight: float | None = None
    rebalance_drift: float = 0.0          # 偏离阈值:|目标-当前权重|≤此值不调(边沿触发);0=关
    cooldown_days: int = 0                # 买入后 N 日内不再加仓;0=关
    min_holding_days: int = 0             # 建仓后 N 个交易日内不卖/不清仓;0=关
    stop_loss_pct: float | None = None    # 浮亏 ≥ 此值豁免 min_holding(软锁:大跌该止损能卖);None=关
    hold_top_percentile: float = 0.0      # 持仓排名在票池前 N% 时不普通卖出;0=关

    def to_dict(self) -> dict[str, Any]:
        return {
            "portfolio_policy_id": self.portfolio_policy_id, "version": self.version,
            "rebalance": self.rebalance,
            "selection": {"method": self.selection.method, "n": self.selection.n,
                          "minimum_score": self.selection.minimum_score},
            "weighting": self.weighting, "max_single_weight": self.max_single_weight,
            "max_gross": self.max_gross, "min_amount_20d": self.min_amount_20d,
            "minimum_listing_days": self.minimum_listing_days,
            "max_industry_weight": self.max_industry_weight,
            "rebalance_drift": self.rebalance_drift,
            "cooldown_days": self.cooldown_days,
            "min_holding_days": self.min_holding_days,
            "stop_loss_pct": self.stop_loss_pct,
            "hold_top_percentile": self.hold_top_percentile,
        }

    def fingerprint(self) -> str:
        return fingerprint(self.to_dict(), prefix="portfolio")


def portfolio_from_dict(d: dict[str, Any]) -> PortfolioPolicy:
    sel = d["selection"]
    hold_top_percentile = float(d.get("hold_top_percentile", 0.0) or 0.0)
    if not 0.0 <= hold_top_percentile <= 1.0:
        raise ValueError("hold_top_percentile 必须在 0 到 1 之间")
    return PortfolioPolicy(
        portfolio_policy_id=str(d["portfolio_policy_id"]), version=int(d["version"]),
        rebalance=str(d.get("rebalance", "monthly")),
        selection=SelectionSpec(method=str(sel.get("method", "top_n_above_score")),
                                n=int(sel["n"]), minimum_score=float(sel["minimum_score"])),
        weighting=str(d.get("weighting", "equal")),
        max_single_weight=float(d.get("max_single_weight", 0.05)),
        max_gross=float(d.get("max_gross", 0.95)),
        min_amount_20d=float(d.get("min_amount_20d", 0.0)),
        minimum_listing_days=int(d.get("minimum_listing_days", 0)),
        max_industry_weight=d.get("max_industry_weight"),
        rebalance_drift=float(d.get("rebalance_drift", 0.0)),
        cooldown_days=int(d.get("cooldown_days", 0)),
        min_holding_days=int(d.get("min_holding_days", 0)),
        stop_loss_pct=d.get("stop_loss_pct"),
        hold_top_percentile=hold_top_percentile,
    )


@dataclass
class DayContext:
    """t 日每只股票的可交易性上下文(engine 组装,portfolio 消费)。"""

    price: dict[str, float]                       # 成交价(raw,用于可交易判断)
    amount_20d: dict[str, float]                  # 近 20 日均成交额
    listing_date: dict[str, date]                 # 上市日
    is_st: dict[str, bool]                        # 是否 ST(点时)
    industry: dict[str, str | None] = field(default_factory=dict)


class PortfolioConstructor:
    """top_n_above_score + 等权 + 容量/集中度 cap。"""

    def __init__(self, policy: PortfolioPolicy) -> None:
        self.policy = policy

    def is_rebalance_day(self, t: date, prev_t: date | None) -> bool:
        r = self.policy.rebalance
        if r == "daily":
            return True
        if prev_t is None:
            return True
        if r == "weekly":
            return t.isocalendar()[:2] != prev_t.isocalendar()[:2]
        if r == "monthly":
            return (t.year, t.month) != (prev_t.year, prev_t.month)
        return True

    def _eligible(self, code: str, score_obs: StrategyScoreObservation,
                  ctx: DayContext, as_of: date) -> bool:
        if score_obs.score_status != ScoreStatus.TRADABLE:
            return False
        if score_obs.strategy_score < self.policy.selection.minimum_score:
            return False
        if ctx.price.get(code, 0.0) <= 0:
            return False
        if self.policy.min_amount_20d > 0 and \
                ctx.amount_20d.get(code, 0.0) < self.policy.min_amount_20d:
            return False
        ld = ctx.listing_date.get(code)
        if ld is not None and self.policy.minimum_listing_days > 0:
            if (as_of - ld).days < self.policy.minimum_listing_days:
                return False
        if ctx.is_st.get(code):
            return False
        return True

    def ranked_candidates(self, scores: dict[str, StrategyScoreObservation],
                          ctx: DayContext, as_of: date) -> list[str]:
        """返回当日组合票池的确定性排名(分数降序、代码升序)。"""
        cand = [c for c in scores if self._eligible(c, scores[c], ctx, as_of)]
        cand.sort(key=lambda c: (-scores[c].strategy_score, c))
        return cand

    def rank_hold_codes(self, scores: dict[str, StrategyScoreObservation],
                        ctx: DayContext, as_of: date) -> set[str]:
        """返回排名保护集合：票池前 hold_top_percentile 的股票。"""
        pct = self.policy.hold_top_percentile
        if pct <= 0.0:
            return set()
        ranked = self.ranked_candidates(scores, ctx, as_of)
        if not ranked:
            return set()
        # 向上取整，保证小票池启用保护后至少保护第一名。
        n = max(1, int(len(ranked) * pct + 0.999999999))
        return set(ranked[:n])

    def select_target(self, scores: dict[str, StrategyScoreObservation],
                      ctx: DayContext, as_of: date) -> dict[str, float]:
        """返回目标权重 dict(只含新建/保留的目标敞口,未含风险覆盖)。"""
        pol = self.policy
        cand = self.ranked_candidates(scores, ctx, as_of)
        picked = cand[: pol.selection.n]
        if not picked:
            return {}
        if pol.weighting == "equal":
            raw = 1.0 / len(picked)
            w = min(raw, pol.max_single_weight)
            weights = {c: w for c in picked}
        else:
            weights = {c: pol.max_single_weight for c in picked}

        # 行业上限是目标权重约束，不改变 alpha 分数和选股排序。
        # 当前首版采用组内等比缩放，保留候选但把行业总敞口压到上限。
        industry_cap = pol.max_industry_weight
        if industry_cap is not None and industry_cap > 0:
            totals: dict[str, float] = {}
            for c, w in weights.items():
                ind = ctx.industry.get(c)
                if ind:
                    totals[ind] = totals.get(ind, 0.0) + w
            for ind, total in totals.items():
                if total > industry_cap:
                    factor = industry_cap / total
                    for c in list(weights):
                        if ctx.industry.get(c) == ind:
                            weights[c] *= factor
        total = sum(weights.values())
        if total > pol.max_gross > 0:
            scale = pol.max_gross / total
            weights = {c: w * scale for c, w in weights.items()}
        return weights
