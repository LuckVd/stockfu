"""52 周新高算子(George & Hwang 2004): 距 52 周高点比例 → score(±20)。

实证:个股「距 52 周高点的近度」对未来收益的预测力**强于**传统动量(过去收益率)——
锚定效应:接近 52 周高点的票,前期套牢盘已消化、上行阻力小,易突破走趋势。
ratio = close / max(close, ~250 日) ∈ (0,1],→1 越强。ratio 0.7→0 分,1.0→+20 分(线性)。
满强度刻度与 momentum/reversal 对齐(±20);rebalancer 横截面排序选近高点个股。
"""
from stockfu.ai.operators.base import BaseOperator, OpResult
from stockfu.ai.operators.registry import register
from stockfu.services.factors import quote_series


@register
class FiftyTwoWeekHighOperator(BaseOperator):
    operator_id = "fifty_two_week_high"
    type = "math"
    PARAMS_SCHEMA = {"lookback": 250, "lo": 0.70}   # 52 周≈250 交易日;ratio 下限锚点

    def run(self, ctx, params):
        lookback = int(params.get("lookback", 250))
        lo = float(params.get("lo", 0.70))
        closes = quote_series(ctx.code, "close", lookback + 30, as_of=ctx.as_of)
        if len(closes) < 60:                          # 至少 ~3 个月才有意义
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning=f"52 周高样本不足({len(closes)})")
        cur = closes[-1]
        high = max(closes)
        if high <= 0:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning="52 周高点无效")
        ratio = cur / high                            # (0,1],→1 越接近新高
        # 线性映射:ratio=lo→0, ratio=1.0→+20(近新高看多);ratio<lo 钳 0(远离不据此做空)
        if ratio >= lo:
            score = 20.0 * (ratio - lo) / (1.0 - lo)
            signal = "buy" if ratio > 0.9 else "hold"
            reasoning = f"距 52 周高点 {ratio * 100:.1f}%(近度高,锚定/突破效应)"
        else:
            score = 0.0
            signal = "hold"
            reasoning = f"距 52 周高点 {ratio * 100:.1f}%(远离,不据此看多)"
        return OpResult(operator=self.operator_id, type="math", value=round(ratio * 100, 2),
                        signal=signal, score=round(score, 1), confidence=0.65,
                        reasoning=reasoning)
