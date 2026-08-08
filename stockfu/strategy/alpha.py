"""alpha 聚合层(设计 §10、§13.1)。

只组合因子分,不决定仓位:
    strategy_score = Σ(a_i · factor_score_i) / Σ(a_i)
    coverage       = Σ(a_i · factor_evidence_coverage_i) / Σ(a_i)

关键(§10.1):factor_score 已按自身 evidence_coverage 向 50 收缩,本层**不再**乘
coverage(防双收缩)。缺失因子用 score=50 自然收缩,coverage 仅作门禁与解释字段。
critical 因子缺失 / coverage < minimum / 有效因子数不足 → score_status=NOT_TRADABLE,
仍输出数值供展示,但组合层不得下单。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from stockfu.scoring.contracts import (
    FactorScoreObservation,
    Maturity,
    ScoreStatus,
    StrategyScoreObservation,
    fingerprint,
)


@dataclass(frozen=True)
class AlphaFactor:
    profile_id: str
    weight: float
    critical: bool


@dataclass(frozen=True)
class AlphaDefinition:
    alpha_id: str
    version: int
    market_scope: str
    factors: tuple[AlphaFactor, ...]
    minimum_coverage: float = 0.70
    minimum_valid_factor_count: int = 1
    # formal 阶段是否必须等待 profile 的历史样本达到 min_observations。
    # force_mature_after_observation 只放开历史成熟度门槛，不伪造缺失 raw。
    formal_maturity_policy: str = "required"

    def weights_by_profile(self) -> dict[str, float]:
        return {f.profile_id: f.weight for f in self.factors}

    def to_dict(self) -> dict[str, Any]:
        return {
            "alpha_id": self.alpha_id, "version": self.version,
            "market_scope": self.market_scope,
            "factors": [{"profile_id": f.profile_id, "weight": f.weight,
                         "critical": f.critical} for f in self.factors],
            "minimum_coverage": self.minimum_coverage,
            "minimum_valid_factor_count": self.minimum_valid_factor_count,
            "formal_maturity_policy": self.formal_maturity_policy,
        }

    def fingerprint(self) -> str:
        return fingerprint(self.to_dict(), prefix="alpha")


def alpha_from_dict(d: dict[str, Any]) -> AlphaDefinition:
    factors = tuple(
        AlphaFactor(profile_id=str(f["profile_id"]),
                    weight=float(f["weight"]),
                    critical=bool(f.get("critical", False)))
        for f in d["factors"])
    if not factors:
        raise ValueError(f"alpha {d.get('alpha_id')}: 至少一个因子")
    if len({f.profile_id for f in factors}) != len(factors):
        raise ValueError(f"alpha {d.get('alpha_id')}: profile_id 不得重复")
    if any(f.weight < 0 for f in factors):
        raise ValueError(f"alpha {d.get('alpha_id')}: 因子权重不得为负")
    wsum = sum(f.weight for f in factors)
    if wsum <= 0:
        raise ValueError(f"alpha {d['alpha_id']}: 权重和需>0")
    minimum_coverage = float(d.get("minimum_coverage", 0.70))
    minimum_valid_factor_count = int(d.get("minimum_valid_factor_count", 1))
    if not 0.0 <= minimum_coverage <= 1.0:
        raise ValueError(f"alpha {d['alpha_id']}: minimum_coverage 必须在[0,1]")
    if minimum_valid_factor_count < 0:
        raise ValueError(f"alpha {d['alpha_id']}: minimum_valid_factor_count 不得为负")
    formal_maturity_policy = str(d.get("formal_maturity_policy", "required"))
    if formal_maturity_policy not in ("required", "force_mature_after_observation"):
        raise ValueError(
            f"alpha {d['alpha_id']}: formal_maturity_policy 必须是 "
            "required 或 force_mature_after_observation"
        )
    a = AlphaDefinition(
        alpha_id=str(d["alpha_id"]), version=int(d["version"]),
        market_scope=str(d.get("market_scope", "cn_equity")),
        factors=factors,
        minimum_coverage=minimum_coverage,
        minimum_valid_factor_count=minimum_valid_factor_count,
        formal_maturity_policy=formal_maturity_policy,
    )
    return a


class AlphaAggregator:
    """把多个 factor_score 聚合成 strategy_score。"""

    def __init__(self, alpha: AlphaDefinition) -> None:
        self.alpha = alpha

    def aggregate(
        self,
        code: str,
        as_of: date,
        factor_scores: dict[str, FactorScoreObservation],
        *,
        reference_cutoff: date,
        universe_status: str = "in_universe",
        observation: bool = False,
    ) -> StrategyScoreObservation:
        a = self.alpha
        wsum = 0.0
        score_acc = 0.0
        cov_acc = 0.0
        valid_count = 0
        critical_missing = False
        immature_block = False
        bypass_formal_maturity = (
            not observation
            and a.formal_maturity_policy == "force_mature_after_observation"
        )
        map_fps: dict[str, str] = {}
        attached: dict[str, FactorScoreObservation] = {}

        for af in a.factors:
            fs = factor_scores.get(af.profile_id)
            w = af.weight
            wsum += w
            if fs is None:
                # 该 profile 未评分:按缺失(50)收缩,evidence=0
                score_acc += w * 50.0
                if af.critical:
                    critical_missing = True
                continue
            map_fps[af.profile_id] = fs.mapping_fingerprint
            attached[af.profile_id] = fs
            score_acc += w * fs.score
            cov_acc += w * fs.evidence_coverage
            evidence_ok = fs.evidence_coverage > 0.0 and (
                not fs.formal_requires_mature or fs.maturity != Maturity.IMMATURE)
            if evidence_ok:
                valid_count += 1
            if af.critical and not evidence_ok:
                critical_missing = True
            if (not observation and fs.formal_requires_mature
                    and fs.maturity != Maturity.MATURE
                    and not bypass_formal_maturity):
                immature_block = True

        strategy_score = (score_acc / wsum) if wsum > 0 else 50.0
        coverage = (cov_acc / wsum) if wsum > 0 else 0.0
        # 浮点收尾
        strategy_score = max(0.0, min(100.0, strategy_score))

        reasons: list[str] = []
        if observation:
            status = ScoreStatus.OBSERVATION
            reasons.append("观察期不交易")
        elif universe_status != "in_universe":
            status = ScoreStatus.NO_UNIVERSE
            reasons.append(f"universe={universe_status}")
        elif critical_missing:
            status = ScoreStatus.NOT_TRADABLE
            reasons.append("关键因子缺失或未成熟")
        elif immature_block:
            status = ScoreStatus.NOT_TRADABLE
            reasons.append("因子历史尚未成熟")
        elif coverage < a.minimum_coverage:
            status = ScoreStatus.NOT_TRADABLE
            reasons.append(f"coverage={coverage:.3f}<{a.minimum_coverage}")
        elif valid_count < a.minimum_valid_factor_count:
            status = ScoreStatus.NOT_TRADABLE
            reasons.append(f"有效因子数 {valid_count}<{a.minimum_valid_factor_count}")
        else:
            status = ScoreStatus.TRADABLE

        return StrategyScoreObservation(
            alpha_id=a.alpha_id, alpha_version=a.version,
            asset_code=code, as_of=as_of,
            strategy_score=strategy_score, effective_coverage=coverage,
            score_status=status, alpha_fingerprint=a.fingerprint(),
            mapping_fingerprints=map_fps, configured_weights=a.weights_by_profile(),
            factor_scores=attached, universe_status=universe_status,
            reference_cutoff=reference_cutoff, reasons=reasons,
        )
