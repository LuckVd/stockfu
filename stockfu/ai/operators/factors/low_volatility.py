"""低波动算子: N 日日收益 std 的历史分位 → score(±20)。低波→正分(防御因子)。

低波动异象(low-volatility anomaly):A 股长期看波动率低的票风险调整后收益更优
(高波动票被过度投机、回撤更深)。取近 window 日日收益率标准差,在近 hist_years
年滚动 std 序列里算时序分位——衡量「当前波动相对自身历史是否异常低」,
再由 rebalancer(cap_and_rank/top_n_picker)做横截面排序选最低波个股。
"""
from stockfu.ai.operators.base import BaseOperator, OpResult
from stockfu.ai.operators.registry import register
from stockfu.services.factors import quote_series, percentile


@register
class LowVolatilityOperator(BaseOperator):
    operator_id = "low_volatility"
    type = "math"
    PARAMS_SCHEMA = {"window": 20, "hist_years": 3}   # 波动窗口 / 历史分位回溯年

    def run(self, ctx, params):
        window = int(params.get("window", 20))
        hist_years = int(params.get("hist_years", 3))
        closes = quote_series(ctx.code, "close", hist_years * 365 + window + 30,
                              as_of=ctx.as_of)
        if len(closes) < window + 1:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning=f"低波样本不足({len(closes)}<{window + 1})")
        rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
        if len(rets) < window:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning="收益率样本不足")

        def _std(seq: list[float]) -> float:
            n = len(seq)
            if n < 2:
                return 0.0
            mean = sum(seq) / n
            return (sum((x - mean) ** 2 for x in seq) / n) ** 0.5

        # 滚动 window 日 std 序列(历史);末位 = 当前 std
        std_series = [_std(rets[i:i + window]) for i in range(len(rets) - window + 1)]
        cur_std = std_series[-1] if std_series else 0.0
        if cur_std <= 0:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning="std 为 0(价格恒定/停牌)")
        pct, n = percentile(std_series, cur_std)
        if pct is None:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning=f"历史 std 样本不足({n})")
        # 低波动 → 正分(pct 越低 = 波动越小 = 越看多);与 value 低估同模板
        if pct < 30:
            score = 20 * (1 - pct / 30)
            signal = "buy"
            reasoning = f"波动率分位 {pct:.0f}% 偏低(低波动异象,风险调整后占优)"
        elif pct > 70:
            score = -20 * (1 - (100 - pct) / 30)
            signal = "sell"
            reasoning = f"波动率分位 {pct:.0f}% 偏高(高波动风险)"
        else:
            score = 0.0
            signal = "hold"
            reasoning = f"波动率分位 {pct:.0f}% 中性"
        return OpResult(operator=self.operator_id, type="math", value=round(pct, 1),
                        signal=signal, score=round(score, 1), confidence=0.6,
                        reasoning=reasoning)
