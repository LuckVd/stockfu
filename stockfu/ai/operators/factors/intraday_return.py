"""日内收益动量因子: 近 N 日日内收益(收/开-1)均值的历史分位 → score(±20)。

实证基础: A 股微观结构实证——「隔夜负收益、日内正风险溢价」(与美股相反)。
与 overnight_return(隔夜,低→正分)互补:日内收益与未来收益正相关,
日内收益越高(开盘后持续走强)的股票,未来收益越占优。
"""
from stockfu.ai.operators.base import BaseOperator, OpResult
from stockfu.ai.operators.registry import register
from stockfu.services.factors import quote_series, percentile


@register
class IntradayReturnOperator(BaseOperator):
    operator_id = "intraday_return"
    type = "math"
    PARAMS_SCHEMA = {"window": 20, "hist_years": 3}   # 日内均值窗口 / 历史分位回溯年

    def run(self, ctx, params):
        window = int(params.get("window", 20))
        hist_years = int(params.get("hist_years", 3))
        opens = quote_series(ctx.code, "open", hist_years * 365 + window + 30,
                             as_of=ctx.as_of, adj="qfq")
        closes = quote_series(ctx.code, "close", hist_years * 365 + window + 30,
                              as_of=ctx.as_of, adj="qfq")
        if len(opens) < window + 1 or len(closes) < window + 1:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning=f"日内样本不足({len(opens)},{len(closes)})")
        n = min(len(opens), len(closes))
        intra = [closes[i] / opens[i] - 1 for i in range(n) if opens[i] > 0]
        if len(intra) < window:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning="日内收益样本不足")

        # 滚动 window 均值序列(历史);末位 = 当前均值。
        cur = sum(intra[-window:]) / window
        means = []
        s = sum(intra[:window])
        means.append(s / window)
        for i in range(window, len(intra)):
            s += intra[i] - intra[i - window]
            means.append(s / window)
        pct, cnt = percentile(means, cur)
        if pct is None:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning=f"历史日内均值样本不足({cnt})")
        # 日内收益高 → 正分(A 股日内正溢价,强者恒强)
        if pct > 70:
            score = 20 * (1 - (100 - pct) / 30)
            signal = "buy"
            reasoning = f"日内收益分位 {pct:.0f}% 偏高(日内正溢价,趋势占优)"
        elif pct < 30:
            score = -20 * (1 - pct / 30)
            signal = "sell"
            reasoning = f"日内收益分位 {pct:.0f}% 偏低(日内走弱)"
        else:
            score = 0.0
            signal = "hold"
            reasoning = f"日内收益分位 {pct:.0f}% 中性"
        return OpResult(operator=self.operator_id, type="math",
                        value=round(cur * 100, 2), signal=signal,
                        score=round(score, 1), confidence=0.6, reasoning=reasoning)
