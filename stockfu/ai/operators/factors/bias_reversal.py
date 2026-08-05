"""乖离率反转因子: 价格偏离均线幅度 BIAS(N) 的历史分位 → score(±20)。

实证基础: 乖离率(BIAS = close/MA(N) - 1)衡量股价相对均线的偏离程度。
A 股实证与实务共识: 负乖离(深度超跌、远离均线下方)后均值回归概率高,
是「抄底」类反转信号;正乖离(过度偏离上方)后回落风险大。
与已有 reversal(短期收益反转)、low_turnover_reversal(换手+反转)不同,
本因子基于「价格-均线距离」,是独立的均值回归技术维度。
"""
from stockfu.ai.operators.base import BaseOperator, OpResult
from stockfu.ai.operators.registry import register
from stockfu.services.factors import quote_series, percentile


@register
class BiasReversalOperator(BaseOperator):
    operator_id = "bias_reversal"
    type = "math"
    PARAMS_SCHEMA = {"window": 20, "hist_years": 3}   # 均线周期 / 历史分位回溯年

    def run(self, ctx, params):
        window = int(params.get("window", 20))
        hist_years = int(params.get("hist_years", 3))
        closes = quote_series(ctx.code, "close", hist_years * 365 + window + 30,
                              as_of=ctx.as_of)
        if len(closes) < window + 1:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning=f"乖离样本不足({len(closes)})")

        # 滚动 BIAS 序列(历史);末位 = 当前 BIAS。增量滑窗维护均线和。
        bias_series = []
        s = sum(closes[:window])
        for i in range(len(closes) - window + 1):
            if i > 0:
                s += closes[i + window - 1] - closes[i - 1]
            ma = s / window
            if ma > 0:
                bias_series.append(closes[i + window - 1] / ma - 1)
        if len(bias_series) < 10:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning="乖离序列样本不足")
        cur = bias_series[-1]
        pct, cnt = percentile(bias_series, cur)
        if pct is None:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning=f"历史乖离样本不足({cnt})")
        # 负乖离(超跌) → 正分(均值回归)
        if pct < 30:
            score = 20 * (1 - pct / 30)
            signal = "buy"
            reasoning = f"乖离率分位 {pct:.0f}%(值 {cur * 100:.1f}%)深度超跌,回归看多"
        elif pct > 70:
            score = -20 * (1 - (100 - pct) / 30)
            signal = "sell"
            reasoning = f"乖离率分位 {pct:.0f}%(值 {cur * 100:.1f}%)过度偏离,回落风险"
        else:
            score = 0.0
            signal = "hold"
            reasoning = f"乖离率分位 {pct:.0f}%(值 {cur * 100:.1f}%)中性"
        return OpResult(operator=self.operator_id, type="math",
                        value=round(cur * 100, 2), signal=signal,
                        score=round(score, 1), confidence=0.6, reasoning=reasoning)
