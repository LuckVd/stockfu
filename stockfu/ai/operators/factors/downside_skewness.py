"""下行偏度因子: 近 N 日日收益偏度(skewness)的历史分位 → score(±20)。

实证基础: 偏度是 A 股定价的重要因子(总偏度/特质偏度,混频分位数回归实证)。
行为金融: 投资者偏好正偏(彩票型)收益 → 正偏股票被高估 → 未来收益低;
负偏(下行偏度大)股票被低估 → 未来收益高。与已有 lottery_max(单日最大涨幅,
一阶矩)不同,本因子用三阶矩衡量收益分布形状,独立增量。

实现: 近 window 日日收益率的三阶标准矩,在近 hist_years 年滚动序列里算时序分位。
"""
from statistics import pstdev

from stockfu.ai.operators.base import BaseOperator, OpResult
from stockfu.ai.operators.registry import register
from stockfu.services.factors import quote_series, percentile


def _skew(xs: list[float]) -> float | None:
    n = len(xs)
    if n < 8:
        return None
    m = sum(xs) / n
    sd = pstdev(xs)
    if sd <= 0:
        return 0.0
    return sum((x - m) ** 3 for x in xs) / n / (sd ** 3)


@register
class DownsideSkewnessOperator(BaseOperator):
    operator_id = "downside_skewness"
    type = "math"
    PARAMS_SCHEMA = {"window": 60, "hist_years": 3}   # 偏度窗口(日收益) / 历史分位回溯年

    def run(self, ctx, params):
        window = int(params.get("window", 60))
        hist_years = int(params.get("hist_years", 3))
        closes = quote_series(ctx.code, "close", hist_years * 365 + window + 30,
                              as_of=ctx.as_of)
        if len(closes) < window + 1:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning=f"偏度样本不足({len(closes)}<{window + 1})")
        rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))
                if closes[i - 1] > 0]
        if len(rets) < window + 8:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning="收益率样本不足")

        # 滚动 window 日偏度序列(历史);增量维护一/二/三阶矩和,每窗口 O(1)。
        # skew = m3 / m2^1.5(中心矩由原始矩 S1/S2/S3 换算)。
        n = window
        s1 = sum(rets[:n])
        s2 = sum(x * x for x in rets[:n])
        s3 = sum(x * x * x for x in rets[:n])
        skews = []
        for i in range(len(rets) - n + 1):
            if i > 0:
                _out, _inn = rets[i - 1], rets[i + n - 1]
                s1 += _inn - _out
                s2 += _inn * _inn - _out * _out
                s3 += _inn ** 3 - _out ** 3
            m1 = s1 / n
            m2 = max(s2 / n - m1 * m1, 0.0)
            if m2 <= 0:
                skews.append(0.0)
                continue
            m3 = s3 / n - 3 * m1 * (s2 / n) + 2 * m1 ** 3
            skews.append(m3 / (m2 ** 1.5))
        if len(skews) < 10:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning="偏度序列样本不足")
        cur = skews[-1]
        pct, cnt = percentile(skews, cur)
        if pct is None:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning=f"历史偏度样本不足({cnt})")
        # 偏度低(负偏,彩票偏好弱) → 正分
        if pct < 30:
            score = 20 * (1 - pct / 30)
            signal = "buy"
            reasoning = f"收益偏度分位 {pct:.0f}% 偏低(负偏/少彩票属性,未被高估)"
        elif pct > 70:
            score = -20 * (1 - (100 - pct) / 30)
            signal = "sell"
            reasoning = f"收益偏度分位 {pct:.0f}% 偏高(彩票型收益,投资者高估)"
        else:
            score = 0.0
            signal = "hold"
            reasoning = f"收益偏度分位 {pct:.0f}% 中性"
        return OpResult(operator=self.operator_id, type="math",
                        value=round(cur, 3), signal=signal,
                        score=round(score, 1), confidence=0.6, reasoning=reasoning)
