"""残差反转因子: 近 N 日市场调整后残差收益均值的历史分位 → score(±20)。

实证基础: Daniel & Titman(2006)《Market Reactions to Tangible and Intangible
Information》——剔除市场与风格暴露后的残差(特质)收益具有反转特性;A 股
实证同样支持:短期(1-4 周)个股相对市场的超额收益呈负自相关(过度反应
→ 均值回归)。与 reversal(绝对收益反转)不同,本因子先剔除市场 β 暴露,
是「特质收益反转」,学术上更干净的翻转度量。

实现: 用近 60 日收益对沪深300 日收益回归估计 β,残差 = r - β×rm;
近 N 日残差均值低 → 正分(超跌反转)。
"""
from stockfu.ai.operators.base import BaseOperator, OpResult
from stockfu.ai.operators.registry import register
from stockfu.services.factors import quote_series, percentile


@register
class ResidualReversalOperator(BaseOperator):
    operator_id = "residual_reversal"
    type = "math"
    PARAMS_SCHEMA = {"window": 20, "beta_window": 60, "hist_years": 3}   # 残差均值窗 / β回归窗 / 回溯年

    def run(self, ctx, params):
        window = int(params.get("window", 20))
        beta_window = int(params.get("beta_window", 60))
        hist_years = int(params.get("hist_years", 3))
        days = hist_years * 365 + window + 30
        closes = quote_series(ctx.code, "close", days, as_of=ctx.as_of)
        mkt = quote_series("sh000300", "close", days, as_of=ctx.as_of)
        if len(closes) < beta_window + window + 1 or len(mkt) < beta_window + window + 1:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning="残差样本不足")
        n = min(len(closes), len(mkt))
        rs = [closes[i] / closes[i - 1] - 1 for i in range(1, n) if closes[i - 1] > 0 and mkt[i - 1] > 0]
        ms = [mkt[i] / mkt[i - 1] - 1 for i in range(1, n) if closes[i - 1] > 0 and mkt[i - 1] > 0]
        if len(rs) < beta_window + window:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning="残差序列样本不足")

        # β 用全样本滚动回归太重;用固定近 beta_window 窗口(当前 β),再对全历史
        # 计算残差均值序列时复用同一 β(近似)。残差均值序列增量滑窗。
        m = sum(ms[-beta_window:]) / beta_window
        r = sum(rs[-beta_window:]) / beta_window
        cov = sum((ms[i] - m) * (rs[i] - r) for i in range(len(rs) - beta_window, len(rs))) / beta_window
        var_m = sum((x - m) ** 2 for x in ms[-beta_window:]) / beta_window
        beta = cov / var_m if var_m > 0 else 0.0

        res = [rs[i] - beta * ms[i] for i in range(len(rs))]
        cur = sum(res[-window:]) / window
        means = []
        s = sum(res[:window])
        means.append(s / window)
        for i in range(window, len(res)):
            s += res[i] - res[i - window]
            means.append(s / window)
        pct, cnt = percentile(means, cur)
        if pct is None:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning=f"历史残差均值样本不足({cnt})")
        # 残差低(跑输市场) → 正分(特质收益反转)
        if pct < 30:
            score = 20 * (1 - pct / 30)
            signal = "buy"
            reasoning = f"残差收益分位 {pct:.0f}%(β={beta:.2f})相对市场超跌,反转看多"
        elif pct > 70:
            score = -20 * (1 - (100 - pct) / 30)
            signal = "sell"
            reasoning = f"残差收益分位 {pct:.0f}%(β={beta:.2f})相对市场超涨,回落风险"
        else:
            score = 0.0
            signal = "hold"
            reasoning = f"残差收益分位 {pct:.0f}%(β={beta:.2f})中性"
        return OpResult(operator=self.operator_id, type="math",
                        value=round(cur * 100, 3), signal=signal,
                        score=round(score, 1), confidence=0.6, reasoning=reasoning)
