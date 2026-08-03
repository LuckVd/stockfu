"""低换手率算子: 近 window 日均换手率 → score(±20)。低换手 → 正分。

A 股最强流动性异象之一:换手率与未来收益**显著负相关**(高换手 = 投机 / 过度自信,未来
跑输;上财《换手率:流动性还是不确定性》、华创证券「换手率因子在中证 800 样本池表现
最佳」)。低换手票机构筹码稳定、波动小,叠合低波与短期反转效应。
均换手 0.5%→+20, 3%→0, 8%→−15(分段线性)。
"""
from stockfu.ai.operators.base import BaseOperator, OpResult
from stockfu.ai.operators.registry import register
from stockfu.services.factors import quote_series


@register
class LowTurnoverOperator(BaseOperator):
    operator_id = "low_turnover"
    type = "math"
    PARAMS_SCHEMA = {"window": 20}   # 均换手窗口

    def run(self, ctx, params):
        window = int(params.get("window", 20))
        turns = quote_series(ctx.code, "turnover", window + 30, as_of=ctx.as_of)
        vals = [t for t in turns[-window:] if t is not None and t > 0]
        if len(vals) < max(window // 2, 5):
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning=f"换手样本不足({len(vals)})")
        avg = sum(vals) / len(vals)                    # %
        if avg <= 3.0:
            score = min(20.0, 20.0 * (3.0 - avg) / 2.5)   # 0.5%→+20(钳), 3%→0
        else:
            score = max(-15.0, -3.0 * (avg - 3.0))     # 3%→0, 8%→−15
        signal = "buy" if avg < 1.5 else "sell" if avg > 5 else "hold"
        return OpResult(operator=self.operator_id, type="math", value=round(avg, 2),
                        signal=signal, score=round(score, 1), confidence=0.6,
                        reasoning=f"近 {window} 日均换手 {avg:.2f}%")
