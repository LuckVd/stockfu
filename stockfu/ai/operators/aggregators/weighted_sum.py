"""加权汇总算子: Σ(score × weight) → total_score → final_signal(无否决)。"""
from stockfu.ai.operators.aggregators.base import Aggregator, collect_meta, score_to_signal
from stockfu.ai.operators.base import OpResult
from stockfu.ai.operators.registry import register


@register
class WeightedSumAggregator(Aggregator):
    operator_id = "weighted_sum"
    type = "aggregator"

    def aggregate(self, results, params):
        th = params.get("thresholds")
        total = round(sum(r.score * r.weight for r in results), 2)
        final = score_to_signal(total, th)
        confidence, ai_tw = collect_meta(results)
        return OpResult(
            operator=self.operator_id, type="aggregator",
            signal=final, score=total,
            confidence=confidence if confidence is not None else 0.5,
            target_weight=ai_tw, veto=False,
            evidence={"total_score": total, "n_ops": len(results)},
            reasoning=f"加权汇总 total={total} → {final}",
        )
