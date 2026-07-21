"""反转算子: N 日收益率取负 → score(±20) + signal。

A 股短期反转效应显著(实证:反转因子比动量更有效)——近期跌幅大的票后续反弹
概率高。本质 = momentum 翻号(score = -ret×2),与 momentum 镜像,满强度 ±20。
短期窗口(5-20 日)反转效应最强;长窗口则趋同动量。
"""
from stockfu.ai.operators.base import BaseOperator, OpResult
from stockfu.ai.operators.registry import register
from stockfu.services.factors import quote_series


@register
class ReversalOperator(BaseOperator):
    operator_id = "reversal"
    type = "math"
    PARAMS_SCHEMA = {"window": 20}   # 反转窗口(交易日;短期反转效应最强)

    def run(self, ctx, params):
        window = int(params.get("window", 20))
        closes = quote_series(ctx.code, "close", window + 30, as_of=ctx.as_of)
        if len(closes) < window + 1:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning=f"反转样本不足({len(closes)}<{window + 1})")
        ret = (closes[-1] / closes[-window] - 1) * 100   # 百分比收益
        score = -ret * 2   # 取负:跌幅越大→正分越强(反转买入);与 momentum 镜像
        signal = "buy" if ret < -3 else "sell" if ret > 3 else "hold"
        return OpResult(operator=self.operator_id, type="math", value=round(ret, 2),
                        signal=signal, score=round(score, 2), confidence=0.7,
                        reasoning=f"{window}日反转(收益 {ret:.2f}% → 反向 score)")
