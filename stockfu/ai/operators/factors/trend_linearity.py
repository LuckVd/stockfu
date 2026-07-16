"""趋势线性度算子: 价格对时间线性回归 r² × 方向 → score(±20) + signal。

参考聚宽"五福闹新春"的 R² 过滤:只追"涨得平稳线性"的趋势,滤掉脉冲式冲高
(动量高但 r² 低 = 鱼尾/伪强势)。与 momentum 正交——
momentum 看"涨多少",trend_linearity 看"涨得稳不稳"。

score = r² × sign(slope) × 40(±20 满强度,对齐 momentum 量纲):
平稳涨(r²→1, slope>0)→ +20;平稳跌 → −20;震荡(r²→0)→ 0。
"""
from stockfu.ai.operators.base import BaseOperator, OpResult
from stockfu.ai.operators.registry import register
from stockfu.services.factors import linreg_r2, quote_series


@register
class TrendLinearityOperator(BaseOperator):
    operator_id = "trend_linearity"
    type = "math"
    PARAMS_SCHEMA = {"window": 20}   # 回归窗口(交易日)

    def run(self, ctx, params):
        window = int(params.get("window", 20))
        closes = quote_series(ctx.code, "close", window + 15, as_of=ctx.as_of)
        if len(closes) < window:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning=f"趋势线性度样本不足({len(closes)}<{window})")
        r2, slope = linreg_r2(closes[-window:])
        direction = 1.0 if slope > 0 else -1.0
        score = round(r2 * direction * 20, 2)   # ±20 满强度:r²=1→±20,对齐 momentum 量纲(weight 0.6→贡献上限 12,不压过主打分)
        signal = ("buy" if r2 > 0.6 and slope > 0
                  else "sell" if r2 > 0.6 and slope < 0
                  else "hold")
        return OpResult(operator=self.operator_id, type="math", value=round(r2, 3),
                        signal=signal, score=score, confidence=round(r2, 2),
                        reasoning=f"{window}日趋势 r²={r2:.2f} slope={slope:+.4f}")
