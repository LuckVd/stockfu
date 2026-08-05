"""MAX 彩票股算子(Bali-Cakici-Whitelaw 2011,反向): 近 window 日最大日收益 → 负分。

彩票偏好(highest-MAX):最大日收益率高的票(暴涨型)被散户当彩票追捧,未来系统性跑输。
反向因子:MAX 高 → 看空(负分);MAX 低不据此看多(效应集中在空头端)。A 股实证
MAX 与换手率均负向预测收益(哈工大《管理科学》;郑振龙-孙清泉 2013 定义「彩票型股票」
= 低价 + 高历史收益 + 高换手)。value = MAX%,阈值外中性。
"""
from stockfu.ai.operators.base import BaseOperator, OpResult
from stockfu.ai.operators.registry import register
from stockfu.services.factors import quote_series


@register
class LotteryMaxOperator(BaseOperator):
    operator_id = "lottery_max"
    type = "math"
    PARAMS_SCHEMA = {"window": 20, "warn_max": 5.0, "flag_max": 8.0}   # MAX 阈值(日收益 %)

    def run(self, ctx, params):
        window = int(params.get("window", 20))
        warn = float(params.get("warn_max", 5.0))
        flag = float(params.get("flag_max", 8.0))
        closes = quote_series(ctx.code, "close", window + 30, as_of=ctx.as_of)
        if len(closes) < window + 1:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning=f"MAX 样本不足({len(closes)})")
        rets = [(closes[i] / closes[i - 1] - 1) * 100 for i in range(-window, 0)]
        mx = max(rets)
        # 仅惩罚高 MAX(彩票型): warn→0, flag→−20 线性;MAX<warn 中性(不强推低 MAX)
        if mx < warn:
            score = 0.0
            signal = "hold"
            reasoning = f"近 {window} 日 MAX {mx:.2f}% 偏低(非彩票型,中性)"
        else:
            score = -20.0 * min(1.0, (mx - warn) / max(flag - warn, 1e-9))
            signal = "sell" if mx >= flag else "hold"
            reasoning = f"近 {window} 日 MAX {mx:.2f}% 偏高(彩票偏好,未来大概率跑输)"
        return OpResult(operator=self.operator_id, type="math", value=round(mx, 2),
                        signal=signal, score=round(score, 1), confidence=0.6,
                        reasoning=reasoning)
