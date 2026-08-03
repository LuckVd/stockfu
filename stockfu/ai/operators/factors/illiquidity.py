"""Amihud 非流动性算子: mean(|日收益| / 成交额) → score(±20)。高非流动 → 正分。

Amihud(2002)经典流动性因子:单位成交额推动的价格变动越大,流动性越差,投资者要求
流动性溢价 → 未来超额收益。ILLIQ = mean(|r_t| / amount_t)。A 股实证与换手率因子高度
相关、控制流动性后换手率效应减弱(《金融研究》李少育 2021)。
value = ILLIQ×1e9(便于阅读);log 归一映射避免极端值主导。
"""
import math

from stockfu.ai.operators.base import BaseOperator, OpResult
from stockfu.ai.operators.registry import register
from stockfu.services.factors import quote_series


@register
class IlliquidityOperator(BaseOperator):
    operator_id = "illiquidity"
    type = "math"
    PARAMS_SCHEMA = {"window": 20}

    def run(self, ctx, params):
        window = int(params.get("window", 20))
        closes = quote_series(ctx.code, "close", window + 30, as_of=ctx.as_of)
        amts = quote_series(ctx.code, "amount", window + 30, as_of=ctx.as_of)
        n = min(len(closes), len(amts))               # 末段对齐(close/amount 同行,过滤一致)
        if n < window + 1:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning=f"Amihud 样本不足({n})")
        closes = closes[-n:]
        amts = amts[-n:]
        ratios = []
        for i in range(n - window, n):
            a = amts[i]
            prev = closes[i - 1]
            if a is None or a <= 0 or prev is None or prev <= 0:
                continue
            r = abs(closes[i] / prev - 1)
            ratios.append(r / a)
        if len(ratios) < max(window // 2, 5):
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning="有效 Amihud 样本不足")
        illiq = sum(ratios) / len(ratios)             # 1/元
        norm = math.log10(illiq * 1e9) if illiq > 0 else -9.0   # 归一化到 ~[-2,2]
        # norm 1→+20(越非流动越看多),0→+10,-1→0,-2→-10
        score = max(-10.0, min(20.0, (norm + 1.0) * 10.0))
        signal = "buy" if norm > 0.5 else "hold"
        return OpResult(operator=self.operator_id, type="math", value=round(illiq * 1e9, 4),
                        signal=signal, score=round(score, 1), confidence=0.55,
                        reasoning=f"Amihud ILLIQ×1e9={illiq * 1e9:.3f}(log={norm:.2f})")
