"""涨停计数因子: 近 N 日涨停(pct_chg≥阈值)次数的历史分位 → score(±20)。

实证基础: A 股涨停(±10%/±20%/ST ±5%)是情绪极端信号。短期涨停次数多 =
连板/妖股特征,筹码博弈激烈、均值回归压力大(与彩票偏好同类,但基于
涨跌停制度而非单日涨幅极值);涨停次数少 = 平淡稳定。与 lottery_max
(单日最大涨幅)相关但基于制度性阈值,独立维度。
"""
from stockfu.ai.operators.base import BaseOperator, OpResult
from stockfu.ai.operators.registry import register
from stockfu.services.factors import quote_series, percentile


@register
class LimitUpCountOperator(BaseOperator):
    operator_id = "limit_up_count"
    type = "math"
    PARAMS_SCHEMA = {"window": 60, "hist_years": 3, "threshold": 9.8}   # 计数窗口 / 回溯年 / 涨停阈值(%)

    def run(self, ctx, params):
        window = int(params.get("window", 60))
        hist_years = int(params.get("hist_years", 3))
        th = float(params.get("threshold", 9.8))
        pcts = quote_series(ctx.code, "pct_chg", hist_years * 365 + window + 30,
                            as_of=ctx.as_of)
        vals = [p for p in pcts if p is not None]
        if len(vals) < window:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning="涨跌幅样本不足")

        # 滚动 window 涨停计数序列;增量滑窗。
        cnt_series = []
        c = sum(1 for v in vals[:window] if v >= th)
        cnt_series.append(c)
        for i in range(window, len(vals)):
            if vals[i - window] >= th:
                c -= 1
            if vals[i] >= th:
                c += 1
            cnt_series.append(c)
        cur = cnt_series[-1]
        pct, cnt_n = percentile(cnt_series, float(cur))
        if pct is None:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning=f"历史涨停计数样本不足({cnt_n})")
        # 涨停多 → 负分(过热,均值回归)
        if pct > 70:
            score = -20 * (1 - (100 - pct) / 30)
            signal = "sell"
            reasoning = f"涨停计数分位 {pct:.0f}%(近{window}日{cur}次)过热,回落风险"
        elif pct < 30:
            score = 20 * (1 - pct / 30)
            signal = "buy"
            reasoning = f"涨停计数分位 {pct:.0f}%(近{window}日{cur}次)平淡,无炒作透支"
        else:
            score = 0.0
            signal = "hold"
            reasoning = f"涨停计数分位 {pct:.0f}%(近{window}日{cur}次)中性"
        return OpResult(operator=self.operator_id, type="math",
                        value=float(cur), signal=signal,
                        score=round(score, 1), confidence=0.6, reasoning=reasoning)
