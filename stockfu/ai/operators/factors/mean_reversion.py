"""均值回归算子: RSI 超买超卖 → 反向 score(±15) + signal。"""
from stockfu.ai.operators.base import BaseOperator, OpResult
from stockfu.ai.operators.registry import register
from stockfu.services.factors import quote_series


def _rsi(closes: list[float], period: int) -> float | None:
    """简单 RSI(period 日,平均涨幅/平均跌幅)。样本不足返回 None。"""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(len(closes) - period, len(closes))]
    gains = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


@register
class MeanReversionOperator(BaseOperator):
    operator_id = "mean_reversion"
    type = "math"
    PARAMS_SCHEMA = {"rsi_period": 14, "oversold": 30, "overbought": 70}

    def run(self, ctx, params):
        period = int(params.get("rsi_period", 14))
        oversold = int(params.get("oversold", 30))
        overbought = int(params.get("overbought", 70))
        closes = quote_series(ctx.code, "close", period + 50, as_of=ctx.as_of)
        rsi = _rsi(closes, period)
        if rsi is None:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning=f"RSI({period})样本不足")
        if rsi < oversold:                                # 超卖→买
            score = 20 * (1 - rsi / oversold)
            signal = "buy"
            reasoning = f"RSI({period})={rsi:.1f} 超卖,反转买入"
        elif rsi > overbought:                            # ��买→卖
            score = -20 * (1 - (100 - rsi) / (100 - overbought))
            signal = "sell"
            reasoning = f"RSI({period})={rsi:.1f} 超买,反转卖出"
        else:
            score = 0.0
            signal = "hold"
            reasoning = f"RSI({period})={rsi:.1f} 中性"
        return OpResult(operator=self.operator_id, type="math", value=round(rsi, 2),
                        signal=signal, score=round(score, 1), confidence=0.6,
                        reasoning=reasoning)
