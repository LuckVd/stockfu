"""下行波动因子: 近 N 日「负收益样本」标准差(半方差)的历史分位 → score(±20)。

实证基础: 下行波动率(semivariance/downside deviation)是风险厌恶投资者真正
关心的风险维度(Sortino 比率的分母);A 股实证:下行波动低的股票后续风险调整
收益更优。与 low_volatility(总波动)不同,本因子只度量下行一侧的波动——
总波动相同但下行波动不同的股票可以区分,是更精细的防御维度。
"""
from stockfu.ai.operators.base import BaseOperator, OpResult
from stockfu.ai.operators.registry import register
from stockfu.services.factors import quote_series, percentile


@register
class DownsideVolatilityOperator(BaseOperator):
    operator_id = "downside_volatility"
    type = "math"
    PARAMS_SCHEMA = {"window": 60, "hist_years": 3}   # 波动窗口 / 历史分位回溯年

    def run(self, ctx, params):
        window = int(params.get("window", 60))
        hist_years = int(params.get("hist_years", 3))
        closes = quote_series(ctx.code, "close", hist_years * 365 + window + 30,
                              as_of=ctx.as_of)
        if len(closes) < window + 1:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning=f"下行波动样本不足({len(closes)})")
        rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))
                if closes[i - 1] > 0]
        if len(rets) < window:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning="收益率样本不足")

        # 滚动 window 日下行 std 序列(只取负收益);增量维护 n_neg/sum/sumsq,每窗口 O(1)。
        n = window
        neg = [x for x in rets[:n] if x < 0]
        k = len(neg)
        s = sum(neg)
        ss = sum(x * x for x in neg)
        down_series = []
        for i in range(len(rets) - n + 1):
            if i > 0:
                _out, _inn = rets[i - 1], rets[i + n - 1]
                for _x, _sgn in ((_out, -1), (_inn, 1)):
                    if _x < 0:
                        k += _sgn
                        s += _sgn * _x
                        ss += _sgn * _x * _x
            var = ss / n - (s / n) ** 2 if n else 0.0
            down_series.append(max(var, 0.0) ** 0.5 if k > 0 else 0.0)
        cur = down_series[-1]
        if cur <= 0:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning="下行波动为 0(价格恒定/停牌)")
        pct, cnt = percentile(down_series, cur)
        if pct is None:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning=f"历史下行波动样本不足({cnt})")
        # 下行波动低 → 正分(防御)
        if pct < 30:
            score = 20 * (1 - pct / 30)
            signal = "buy"
            reasoning = f"下行波动分位 {pct:.0f}% 偏低(下行风险小,防御占优)"
        elif pct > 70:
            score = -20 * (1 - (100 - pct) / 30)
            signal = "sell"
            reasoning = f"下行波动分位 {pct:.0f}% 偏高(下行风险大)"
        else:
            score = 0.0
            signal = "hold"
            reasoning = f"下行波动分位 {pct:.0f}% 中性"
        return OpResult(operator=self.operator_id, type="math",
                        value=round(cur * 100, 3), signal=signal,
                        score=round(score, 1), confidence=0.6, reasoning=reasoning)
