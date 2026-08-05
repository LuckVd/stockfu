"""时序动量算子(风险调整动量 / TSMOM): 收益÷波动 → score(±20)。

区别于横截面动量(比谁涨得多):时序动量看「自身趋势是否成立」并用波动率归一
(Moskowitz-Ooi-Pedersen 2012 的 TSMOM 核心 = sign(过去收益) × 波动率倒数仓位)。
z = ret / (vol·√window) ≈ 漂移的 t 统计量:单位风险的趋势强度,自动惩罚暴涨暴跌型
高波动票(同样涨幅、波动更小者 z 更高)。ret 用 window 日累计收益,vol 为同窗日收益 std。
满强度 ±20:z ≈ ±2 → ∓20(2σ 趋势)。
"""
import math

from stockfu.ai.operators.base import BaseOperator, OpResult
from stockfu.ai.operators.registry import register
from stockfu.services.factors import quote_series


@register
class TsMomentumOperator(BaseOperator):
    operator_id = "ts_momentum"
    type = "math"
    PARAMS_SCHEMA = {"window": 120}   # 中长期趋势窗(120 日≈半年)

    def run(self, ctx, params):
        window = int(params.get("window", 120))
        # 日历日缓冲:window 交易日需 ~window×1.5 日历日(245/365≈0.67);+30 余量
        closes = quote_series(ctx.code, "close", int(window * 1.5) + 30, as_of=ctx.as_of)
        if len(closes) < window + 1:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning=f"时序动量样本不足({len(closes)}<{window + 1})")
        ret = (closes[-1] / closes[-window] - 1) * 100
        rets = [closes[i] / closes[i - 1] - 1 for i in range(len(closes) - window, len(closes))]
        if len(rets) < 2:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning="波动样本不足")
        mean = sum(rets) / len(rets)
        vol = (sum((r - mean) ** 2 for r in rets) / len(rets)) ** 0.5 * 100   # 日 std→百分数
        if vol <= 0:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning="波动为 0(停牌/恒定)")
        z = ret / (vol * math.sqrt(window))           # 漂移 t 统计量
        score = max(-20.0, min(20.0, z * 10.0))       # z≈±2 → ∓20
        signal = "buy" if z > 0.5 else "sell" if z < -0.5 else "hold"
        return OpResult(operator=self.operator_id, type="math", value=round(z, 3),
                        signal=signal, score=round(score, 1), confidence=0.65,
                        reasoning=f"{window} 日时序动量 ret{ret:.2f}%/vol{vol:.2f}% → z={z:.2f}")
