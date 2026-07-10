"""趋势强度算子: MA5/10/20 多空排列 → score(±20) + signal。"""
from stockfu.ai.operators.base import BaseOperator, OpResult
from stockfu.ai.operators.registry import register
from stockfu.services.factors import ma_alignment


@register
class TrendStrengthOperator(BaseOperator):
    operator_id = "trend_strength"
    type = "math"

    def run(self, ctx, params):
        ali = ma_alignment(ctx.code, lookback=250, as_of=ctx.as_of)
        if ali is None:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning="MA 排列样本不足")
        if ali == "bullish":
            return OpResult(operator=self.operator_id, type="math", value=1,
                            signal="buy", score=20.0, confidence=0.7,
                            reasoning="多头排列(MA5>MA10>MA20)")
        if ali == "bearish":
            return OpResult(operator=self.operator_id, type="math", value=-1,
                            signal="sell", score=-20.0, confidence=0.7,
                            reasoning="空头排列(MA5<MA10<MA20)")
        return OpResult(operator=self.operator_id, type="math", value=0,
                        signal="hold", score=0.0, confidence=0.5,
                        reasoning="MA 中性/交叉")
