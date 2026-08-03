"""唐奇安通道突破算子: 通道位置 → score(±20)。

唐奇安通道 = 近 N 日最高/最低;海龟交易系统核心。突破上轨(接近/创 N 日新高)→ 看多
趋势确立;跌破下轨 → 看空。区别于布林带(均值 ± std 做均值回归):唐奇安是纯突破/趋势
跟随。pos = (close − lower) / (upper − lower) ∈ [0,1];>0.8 → +20(突破买),<0.2 → −20
(破位卖)。通道用「不含当日的近 window 日 close」(突破判定避免自指),close 口径简洁
且与 high/low 单日缺失解耦。
"""
from stockfu.ai.operators.base import BaseOperator, OpResult
from stockfu.ai.operators.registry import register
from stockfu.services.factors import quote_series


@register
class DonchianBreakoutOperator(BaseOperator):
    operator_id = "donchian_breakout"
    type = "math"
    PARAMS_SCHEMA = {"window": 20, "buy_pos": 0.8, "sell_pos": 0.2}

    def run(self, ctx, params):
        window = int(params.get("window", 20))
        buy_pos = float(params.get("buy_pos", 0.8))
        sell_pos = float(params.get("sell_pos", 0.2))
        closes = quote_series(ctx.code, "close", window + 30, as_of=ctx.as_of)
        if len(closes) < window + 1:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning=f"唐奇安样本不足({len(closes)})")
        cur = closes[-1]
        prior = closes[-window - 1:-1]                # 近 window 日(不含当日)
        upper = max(prior)
        lower = min(prior)
        if upper <= lower:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning="通道退化(高 = 低)")
        pos = (cur - lower) / (upper - lower)
        # 线性映射:pos=0.5→0, 1.0→+20, 0.0→−20
        score = max(-20.0, min(20.0, 40.0 * (pos - 0.5)))
        signal = "buy" if pos > buy_pos else "sell" if pos < sell_pos else "hold"
        return OpResult(operator=self.operator_id, type="math", value=round(pos * 100, 1),
                        signal=signal, score=round(score, 1), confidence=0.6,
                        reasoning=f"唐奇安{window} 日 位置 {pos * 100:.0f}%(上轨突破/下轨破位)")
