"""量能枯竭反转因子: 近 short 日均成交额 / 近 long 日均成交额的历史分位 → score(±20)。

实证基础: 成交额(量能)是 A 股情绪的直接度量。缩量到极致 = 抛压枯竭、
多空分歧收敛,是阶段性底部的典型特征;放量到极致 = 情绪过热/换手加速,
短期见顶风险大。与已有 low_turnover(换手率水平,截面横向比较)不同,
本因子度量「量能的时间序列趋势」(个股自身当前量 vs 自身历史均量),独立维度。
"""
from stockfu.ai.operators.base import BaseOperator, OpResult
from stockfu.ai.operators.registry import register
from stockfu.services.factors import quote_series, percentile


@register
class VolumeDroughtOperator(BaseOperator):
    operator_id = "volume_drought"
    type = "math"
    PARAMS_SCHEMA = {"short": 5, "long": 120, "hist_years": 3}   # 短/长均量窗口 / 历史分位回溯年

    def run(self, ctx, params):
        short = int(params.get("short", 5))
        long_ = int(params.get("long", 120))
        hist_years = int(params.get("hist_years", 3))
        amts = quote_series(ctx.code, "amount", hist_years * 365 + long_ + 30,
                            as_of=ctx.as_of)
        amts = [a for a in amts if a is not None and a > 0]
        if len(amts) < long_ + 1:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning=f"量能样本不足({len(amts)})")

        # 滚动 短/长均量比 序列(历史);末位 = 当前比值。增量滑窗维护两个和。
        ratio_series = []
        s_short = sum(amts[:short])
        s_long = sum(amts[:long_])
        for i in range(long_, len(amts)):
            # 窗口 [i-long_+1, i] 的长均量 与 [i-short+1, i] 的短均量;两和每次同步滑动
            s_long += amts[i] - amts[i - long_]
            s_short += amts[i] - amts[i - short]
            if s_long > 0:
                ratio_series.append((s_short / short) / (s_long / long_))
        if len(ratio_series) < 10:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning="量能比值序列样本不足")
        cur = ratio_series[-1]
        pct, cnt = percentile(ratio_series, cur)
        if pct is None:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning=f"历史量能比值样本不足({cnt})")
        # 量能枯竭(比值低) → 正分(抛压枯竭,反转)
        if pct < 30:
            score = 20 * (1 - pct / 30)
            signal = "buy"
            reasoning = f"量能分位 {pct:.0f}%(短/长均量 {cur:.2f})枯竭,抛压收敛看多"
        elif pct > 70:
            score = -20 * (1 - (100 - pct) / 30)
            signal = "sell"
            reasoning = f"量能分位 {pct:.0f}%(短/长均量 {cur:.2f})过热,情绪见顶风险"
        else:
            score = 0.0
            signal = "hold"
            reasoning = f"量能分位 {pct:.0f}%(短/长均量 {cur:.2f})中性"
        return OpResult(operator=self.operator_id, type="math",
                        value=round(cur, 3), signal=signal,
                        score=round(score, 1), confidence=0.6, reasoning=reasoning)
