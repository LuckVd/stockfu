"""动量算子: N 日收益率 → score(±10) + signal。"""
from stockfu.ai.operators.base import BaseOperator, OpResult
from stockfu.ai.operators.registry import register
from stockfu.services.factors import quote_series


@register
class MomentumOperator(BaseOperator):
    operator_id = "momentum"
    type = "math"
    PARAMS_SCHEMA = {"window": 20}   # 动量窗口(交易日)

    def run(self, ctx, params):
        window = int(params.get("window", 20))
        closes = quote_series(ctx.code, "close", window + 30, as_of=ctx.as_of)
        if len(closes) < window + 1:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning=f"动量样本不足({len(closes)}<{window + 1})")
        ret = (closes[-1] / closes[-window] - 1) * 100   # 百分比收益
        score = ret * 2                                    # 连续强度(不 clamp);1%→2分,满强度±20=±10%
        signal = "buy" if ret > 3 else "sell" if ret < -3 else "hold"
        return OpResult(operator=self.operator_id, type="math", value=round(ret, 2),
                        signal=signal, score=round(score, 2), confidence=0.7,
                        reasoning=f"{window}日动量 {ret:.2f}%")
