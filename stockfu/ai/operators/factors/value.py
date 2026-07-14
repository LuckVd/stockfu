"""价值算子: PE 近10年历史分位 → score(±10) + signal。低估买/高估卖。

PE 分位复用 services.valuation.valuation_percentile(纯 DB、无网络、无未来函数,
严格 <=as_of),算子自身不裸 SQL——取数收口在服务层(契约:算子不直接取库)。
"""
from datetime import date

from stockfu.ai.operators.base import BaseOperator, OpResult
from stockfu.ai.operators.registry import register
from stockfu.services.valuation import valuation_percentile


@register
class ValueOperator(BaseOperator):
    operator_id = "value"
    type = "math"

    def run(self, ctx, params):
        # PE 近10年历史分位(复用 services.valuation,纯 DB、无未来函数)
        pct, _pb = valuation_percentile(ctx.code, ctx.as_of or date.today(), years=10)
        if pct is None:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning="PE 分位样本不足")
        if pct < 20:                                     # 低估→买
            raw = 20 * (1 - pct / 20)                      # ±10→±20(统一对齐 LLM)
            score = max(5.0, raw)
            signal = "buy"
            reasoning = f"PE 分位 {pct:.0f}% 偏低,估值有吸引力"
        elif pct > 80:                                   # 高估→卖
            raw = -20 * (1 - (100 - pct) / 20)
            score = min(-5.0, raw)
            signal = "sell"
            reasoning = f"PE 分位 {pct:.0f}% 偏高,估值偏贵"
        else:
            raw = 0.0
            score = 0.0
            signal = "hold"
            reasoning = f"PE 分位 {pct:.0f}% 合理区间"
        return OpResult(operator=self.operator_id, type="math", value=round(pct, 1),
                        signal=signal, score=round(score, 1), raw_score=round(raw, 2), confidence=0.6,
                        reasoning=reasoning)
