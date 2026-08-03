"""动量加速算子(二阶动量): 近段收益 − 远段收益 → score(±20)。

一阶动量只看「涨多少」,加速动量看「涨得是否在加速」。accel = 近半窗收益 − 远半窗收益:
正值 = 趋势加速(近期比早期强,动能在增强)→ 看多;负值 = 减速/见顶迹象 → 看空。
配合一阶动量用(加速 + 正动量 = 强势确认;加速 + 负动量 = 拐点预警)。窗口默认 120 日
(近/远各 60)。1% 加速 ≈ 1.5 分(±13% 饱和)。
"""
from stockfu.ai.operators.base import BaseOperator, OpResult
from stockfu.ai.operators.registry import register
from stockfu.services.factors import quote_series


@register
class MomentumAccelerationOperator(BaseOperator):
    operator_id = "momentum_acceleration"
    type = "math"
    PARAMS_SCHEMA = {"window": 120}   # 全窗;近/远各 window/2

    def run(self, ctx, params):
        window = int(params.get("window", 120))
        half = max(window // 2, 5)
        need = 2 * half + 1
        # 日历日缓冲:need 交易日需 ~need×1.5 日历日;+30 余量
        closes = quote_series(ctx.code, "close", int(need * 1.5) + 30, as_of=ctx.as_of)
        if len(closes) < need:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning=f"加速动量样本不足({len(closes)}<{need})")
        recent = (closes[-1] / closes[-half - 1] - 1) * 100       # 近 half 日收益
        older = (closes[-half - 1] / closes[-2 * half - 1] - 1) * 100  # 远 half 日收益
        accel = recent - older
        score = max(-20.0, min(20.0, accel * 1.5))
        signal = "buy" if accel > 3 else "sell" if accel < -3 else "hold"
        return OpResult(operator=self.operator_id, type="math", value=round(accel, 2),
                        signal=signal, score=round(score, 1), confidence=0.6,
                        reasoning=f"动量加速 近{recent:.2f}% − 远{older:.2f}% = {accel:+.2f}%")
