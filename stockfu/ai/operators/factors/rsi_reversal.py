"""RSI 超卖反转因子: RSI(N) 的历史分位 → score(±20)。

实证基础: RSI(相对强弱指标)在单边行情中极端值往往是反转信号(超卖→反弹、
超买→回落),可扩展为横截面选股因子(A 股实证:RSI 低 → 后续收益高)。

与已测风格的区别: 已有反转因子(reversal/low_turnover_reversal)基于收益/换手;
本因子基于 RSI(涨跌幅平滑后的相对强弱),是独立的技术指标维度。
"""
from stockfu.ai.operators.base import BaseOperator, OpResult
from stockfu.ai.operators.registry import register
from stockfu.services.factors import quote_series, percentile


def _rsi_series(closes: list[float], n: int) -> list[float]:
    """增量 Wilder RSI 序列(每新增一日 O(1),整段 O(len))。返回 RSI 序列(0-100)。"""
    if len(closes) < n + 1:
        return []
    diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    avg_g = sum(max(d, 0.0) for d in diffs[:n]) / n
    avg_l = sum(max(-d, 0.0) for d in diffs[:n]) / n
    out = []
    for d in diffs[n:]:
        avg_g = (avg_g * (n - 1) + max(d, 0.0)) / n
        avg_l = (avg_l * (n - 1) + max(-d, 0.0)) / n
        out.append(100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l))
    return out


@register
class RsiReversalOperator(BaseOperator):
    operator_id = "rsi_reversal"
    type = "math"
    PARAMS_SCHEMA = {"window": 14, "hist_years": 3}   # RSI 周期 / 历史分位回溯年

    def run(self, ctx, params):
        window = int(params.get("window", 14))
        hist_years = int(params.get("hist_years", 3))
        closes = quote_series(ctx.code, "close", hist_years * 365 + window + 30,
                              as_of=ctx.as_of)
        if len(closes) < window + 1:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning=f"RSI 样本不足({len(closes)})")

        # 增量滚动 RSI 序列(历史);末位 = 当前 RSI。
        rsi_series = _rsi_series(closes, window)
        if len(rsi_series) < 10:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning="RSI 序列样本不足")
        cur = rsi_series[-1]
        pct, cnt = percentile(rsi_series, cur)
        if pct is None:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning=f"历史 RSI 样本不足({cnt})")
        # RSI 低(超卖) → 正分(反转)
        if pct < 30:
            score = 20 * (1 - pct / 30)
            signal = "buy"
            reasoning = f"RSI 分位 {pct:.0f}%(值 {cur:.1f})超卖,反转看多"
        elif pct > 70:
            score = -20 * (1 - (100 - pct) / 30)
            signal = "sell"
            reasoning = f"RSI 分位 {pct:.0f}%(值 {cur:.1f})超买,反转看空"
        else:
            score = 0.0
            signal = "hold"
            reasoning = f"RSI 分位 {pct:.0f}%(值 {cur:.1f})中性"
        return OpResult(operator=self.operator_id, type="math",
                        value=round(cur, 1), signal=signal,
                        score=round(score, 1), confidence=0.6, reasoning=reasoning)
