"""隔夜收益因子: 近 N 日隔夜收益(今开/昨收-1)均值的历史分位 → score(±20)。

实证基础: A 股长期存在「隔夜负收益、日内正风险溢价」的微观结构特征(与美股
相反;Lou-Polk-Skouras 2019 的 A 股实证)。隔夜收益与未来收益负相关——
隔夜收益越低(开盘相对昨收折价越多)的股票,未来收益越高。
本项目已测风格(红利/低波/价值/动量/小盘/低换手/彩票/趋势)均不涉及
「隔夜 vs 日内收益分解」,本因子是全新维度。

口径: 用前复权(qfq)价格算隔夜收益,规避除息跳空;qfq 走回测内存预载,零 DB。
"""
from stockfu.ai.operators.base import BaseOperator, OpResult
from stockfu.ai.operators.registry import register
from stockfu.services.factors import quote_series, percentile


@register
class OvernightReturnOperator(BaseOperator):
    operator_id = "overnight_return"
    type = "math"
    PARAMS_SCHEMA = {"window": 20, "hist_years": 3}   # 隔夜均值窗口 / 历史分位回溯年

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
                            reasoning=f"隔夜样本不足({len(opens)},{len(closes)})")
        # opens/closes 同窗口同过滤,逐日对齐(同表同日)。隔夜收益 = open_i/close_{i-1}-1
        n = min(len(opens), len(closes))
        on = [opens[i] / closes[i - 1] - 1 for i in range(1, n) if closes[i - 1] > 0]
        if len(on) < window:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning="隔夜收益样本不足")

        # 滚动 window 均值序列(历史);末位 = 当前均值。
        cur = sum(on[-window:]) / window
        means = []
        s = sum(on[:window])
        means.append(s / window)
        for i in range(window, len(on)):
            s += on[i] - on[i - window]
            means.append(s / window)
        pct, cnt = percentile(means, cur)
        if pct is None:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning=f"历史隔夜均值样本不足({cnt})")
        # 隔夜收益低 → 正分(A 股负隔夜溢价:低隔夜收益预测高未来收益)
        if pct < 30:
            score = 20 * (1 - pct / 30)
            signal = "buy"
            reasoning = f"隔夜收益分位 {pct:.0f}% 偏低(负隔夜溢价,未来收益占优)"
        elif pct > 70:
            score = -20 * (1 - (100 - pct) / 30)
            signal = "sell"
            reasoning = f"隔夜收益分位 {pct:.0f}% 偏高(隔夜溢价透支)"
        else:
            score = 0.0
            signal = "hold"
            reasoning = f"隔夜收益分位 {pct:.0f}% 中性"
        return OpResult(operator=self.operator_id, type="math",
                        value=round(cur * 100, 2), signal=signal,
                        score=round(score, 1), confidence=0.6, reasoning=reasoning)
