"""风险一票否决汇总: 在加权汇总基础上,任一算子 veto=True(risk 顾问 sell)→强制卖出。

复现 synthesis.aggregate 的 risk_vetoed 逻辑:risk 顾问 strong_sell→strong_sell,
sell→sell,压过所有看多。
"""
from stockfu.ai.operators.aggregators.weighted_sum import WeightedSumAggregator
from stockfu.ai.operators.registry import register


@register
class RiskVetoAggregator(WeightedSumAggregator):
    operator_id = "risk_veto"
    type = "aggregator"

    def aggregate(self, results, params):
        base = super().aggregate(results, params)
        vetoed = [r for r in results if r.veto]
        if not vetoed:
            return base
        # risk 顾问一票否决:strong_sell→strong_sell, 否则 sell
        risk = next((r for r in results if r.operator == "risk"), None)
        base.signal = "strong_sell" if (risk and risk.signal == "strong_sell") else "sell"
        base.veto = True
        base.reasoning = (base.reasoning + " | " if base.reasoning else "") + "risk 一票否决"
        return base
