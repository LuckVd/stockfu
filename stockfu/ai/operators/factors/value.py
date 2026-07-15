"""价值算子: PE 历史分位 → 连续 score + 派生 signal。低估买/高估卖。

PE 分位复用 services.valuation.valuation_percentile(纯 DB、无网络、无未来函数,
严格 <=as_of)。窗口默认 5 年——与 baostock 落库深度对齐(约 2021 起),勿假装 10 年。
"""
from datetime import date

from stockfu.ai.operators.base import BaseOperator, OpResult
from stockfu.ai.operators.registry import register
from stockfu.services.valuation import valuation_percentile


@register
class ValueOperator(BaseOperator):
    operator_id = "value"
    type = "math"
    PARAMS_SCHEMA = {"years": 5}   # 与数据深度对齐;可调但勿超过库内覆盖

    def run(self, ctx, params):
        years = int(params.get("years", 5))
        pct, _pb = valuation_percentile(ctx.code, ctx.as_of or date.today(), years=years)
        if pct is None:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning="PE 分位样本不足")
        if pct < 20:                                     # 低估→买
            score = 20 * (1 - pct / 20)                    # 连续强度(不 clamp);分位越低越强
            signal = "buy"
            reasoning = f"PE 分位 {pct:.0f}% 偏低,估值有吸引力"
        elif pct > 80:                                   # 高估→卖
            score = -20 * (1 - (100 - pct) / 20)
            signal = "sell"
            reasoning = f"PE 分位 {pct:.0f}% 偏高,估值偏贵"
        else:
            score = 0.0
            signal = "hold"
            reasoning = f"PE 分位 {pct:.0f}% 合理区间"
        return OpResult(operator=self.operator_id, type="math", value=round(pct, 1),
                        signal=signal, score=round(score, 1), confidence=0.6,
                        reasoning=reasoning)
