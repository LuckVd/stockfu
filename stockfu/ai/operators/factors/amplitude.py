"""振幅因子: 近 N 日振幅((high-low)/close)均值的历史分位 → score(±20)。

实证基础: 振幅(日内极差)是高频波动的直接度量,与收益率标准差相关但包含
日内高低点信息(隔夜跳空不贡献振幅)。低振幅 = 多空分歧小、筹码稳定,
A 股实证低振幅股票风险调整后收益更优。与 low_volatility(收盘价波动)互补。
"""
from stockfu.ai.operators.base import BaseOperator, OpResult
from stockfu.ai.operators.registry import register
from stockfu.services.factors import quote_series, percentile


@register
class AmplitudeOperator(BaseOperator):
    operator_id = "amplitude"
    type = "math"
    PARAMS_SCHEMA = {"window": 20, "hist_years": 3}   # 振幅窗口 / 历史分位回溯年

    def run(self, ctx, params):
        window = int(params.get("window", 20))
        hist_years = int(params.get("hist_years", 3))
        days = hist_years * 365 + window + 30
        highs = quote_series(ctx.code, "high", days, as_of=ctx.as_of, adj="qfq")
        lows = quote_series(ctx.code, "low", days, as_of=ctx.as_of, adj="qfq")
        closes = quote_series(ctx.code, "close", days, as_of=ctx.as_of, adj="qfq")
        n = min(len(highs), len(lows), len(closes))
        amps = []
        for i in range(n):
            if closes[i] > 0 and highs[i] is not None and lows[i] is not None:
                amps.append((highs[i] - lows[i]) / closes[i])
        if len(amps) < window:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning="振幅样本不足")

        # 滚动 window 均值序列(历史);末位 = 当前均值。
        cur = sum(amps[-window:]) / window
        means = []
        s = sum(amps[:window])
        means.append(s / window)
        for i in range(window, len(amps)):
            s += amps[i] - amps[i - window]
            means.append(s / window)
        pct, cnt = percentile(means, cur)
        if pct is None:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning=f"历史振幅样本不足({cnt})")
        # 低振幅 → 正分(稳定)
        if pct < 30:
            score = 20 * (1 - pct / 30)
            signal = "buy"
            reasoning = f"振幅分位 {pct:.0f}% 偏低(日内波动小,筹码稳定)"
        elif pct > 70:
            score = -20 * (1 - (100 - pct) / 30)
            signal = "sell"
            reasoning = f"振幅分位 {pct:.0f}% 偏高(日内博弈激烈)"
        else:
            score = 0.0
            signal = "hold"
            reasoning = f"振幅分位 {pct:.0f}% 中性"
        return OpResult(operator=self.operator_id, type="math",
                        value=round(cur * 100, 2), signal=signal,
                        score=round(score, 1), confidence=0.6, reasoning=reasoning)
