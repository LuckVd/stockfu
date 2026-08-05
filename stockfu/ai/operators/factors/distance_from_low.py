"""52 周低点距离因子: 收盘价相对 252 日最低点的距离的历史分位 → score(±20)。

实证基础: 反转效应(Reversal Effect)在 A 股长期存在(1997-2016 月频实证)。
与已测 fifty_two_week_high(距 52 周高点,追涨动量)相反:距 52 周低点越近,
悲观情绪定价越充分,均值回归的潜在空间越大;而 52 周高点因子在 A 股满仓
无止损配置下已证伪(追高接盘)。本因子是「超跌→反转」维度的独立测度。

注意: 距低点近 ≠ 一定反转(趋势延续风险),是否成立由回测判定。
"""
from stockfu.ai.operators.base import BaseOperator, OpResult
from stockfu.ai.operators.registry import register
from stockfu.services.factors import quote_series, percentile


@register
class DistanceFromLowOperator(BaseOperator):
    operator_id = "distance_from_low"
    type = "math"
    PARAMS_SCHEMA = {"window": 252, "hist_years": 3}   # 低点回溯窗口 / 历史分位回溯年

    def run(self, ctx, params):
        window = int(params.get("window", 252))
        hist_years = int(params.get("hist_years", 3))
        closes = quote_series(ctx.code, "close", hist_years * 365 + window + 30,
                              as_of=ctx.as_of)
        if len(closes) < window + 1:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning=f"低点样本不足({len(closes)})")

        # 滚动「距 window 日最低价距离」序列;末位 = 当前距离。增量维护窗口最低价
        # (滑窗最小值,均摊 O(1))。
        dist_series = []
        from collections import deque
        dq = deque()  # 单调递增队列存索引,维护滑窗最小值
        for i in range(len(closes)):
            while dq and closes[dq[-1]] >= closes[i]:
                dq.pop()
            dq.append(i)
            if dq[0] <= i - window:
                dq.popleft()
            if i >= window - 1 and closes[dq[0]] > 0:
                dist_series.append(closes[i] / closes[dq[0]] - 1)
        if len(dist_series) < 10:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning="低点距离序列样本不足")
        cur = dist_series[-1]
        pct, cnt = percentile(dist_series, cur)
        if pct is None:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning=f"历史低点距离样本不足({cnt})")
        # 距低点近(距离小) → 正分(超跌反转)
        if pct < 30:
            score = 20 * (1 - pct / 30)
            signal = "buy"
            reasoning = f"距52周低点距离分位 {pct:.0f}%(+{cur * 100:.1f}%)贴近低点,反转看多"
        elif pct > 70:
            score = -20 * (1 - (100 - pct) / 30)
            signal = "sell"
            reasoning = f"距52周低点距离分位 {pct:.0f}%(+{cur * 100:.1f}%)远离低点,追高风险"
        else:
            score = 0.0
            signal = "hold"
            reasoning = f"距52周低点距离分位 {pct:.0f}%(+{cur * 100:.1f}%)中性"
        return OpResult(operator=self.operator_id, type="math",
                        value=round(cur * 100, 2), signal=signal,
                        score=round(score, 1), confidence=0.6, reasoning=reasoning)
