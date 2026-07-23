"""日线布林带算子(均值回归):价格在布林带中的位置 → 多空信号。

真正的"中下轨买、中上轨卖"均值回归:
  - 跌破下轨 / 中下轨(position<buy_max)→ 买,越接近下轨越强
  - 突破上轨 / 中上轨(position>sell_min)→ 卖,越接近上轨越强
  - 中轨死区(buy_max..sell_min)→ 观望

与 monthly_bollinger / weekly_bollinger 的区别:用日线 close 直接算(无月/周聚合),
信号更灵敏、更适合波段均值回归。位置阈值 buy_max/sell_min 全参数化(默认 0.45/0.55,
中轨两侧各留 5% 死区),供策略按需调整。

无未来函数:取数走 quote_series(..., as_of=ctx.as_of),严格 <=as_of。
"""

from stockfu.ai.operators.base import BaseOperator, OpResult
from stockfu.ai.operators.factors.monthly_bollinger import _calc_bollinger
from stockfu.ai.operators.registry import register
from stockfu.services.factors import quote_series


def _position_score(latest_close: float, upper: float, lower: float,
                    buy_max: float, sell_min: float):
    """根据价格在布林带中的位置生成信号(中下轨买 / 中上轨卖)。

    position: 0=下轨, 0.5≈中轨, 1=上轨。
    返回 (signal, score, confidence, detail)。score=连续强度(不 clamp);越接近/超出轨道越强。
    """
    band_range = upper - lower if upper > lower else 1.0
    position = (latest_close - lower) / band_range

    if latest_close <= lower:                                   # 跌破下轨 → 强买
        exceed = (lower - latest_close) / band_range
        score = 12.0 + exceed * 15.0
        signal = "buy" if exceed < 0.3 else "strong_buy"
        confidence = min(0.85, 0.6 + abs(score) / 40.0)
        detail = f"日线跌破下轨(超出{exceed:.1%}),极度超卖"
    elif latest_close >= upper:                                 # 突破上轨 → 强卖
        exceed = (latest_close - upper) / band_range
        score = -12.0 - exceed * 15.0
        signal = "sell" if exceed < 0.3 else "strong_sell"
        confidence = min(0.85, 0.6 + abs(score) / 40.0)
        detail = f"日线突破上轨(超出{exceed:.1%}),极度超买"
    elif position < buy_max:                                    # 中下轨 → 买
        score = 10.0 * (1.0 - position / buy_max)               # 下轨处=10, buy_max处=0
        signal = "buy"
        confidence = 0.6
        detail = f"日线下半区(位置{position:.0%}<{buy_max:.0%}),超卖回升区"
    elif position > sell_min:                                   # 中上轨 → 卖
        score = -10.0 * ((position - sell_min) / (1.0 - sell_min))
        signal = "sell"
        confidence = 0.6
        detail = f"日线上半区(位置{position:.0%}>{sell_min:.0%}),超买回落区"
    else:                                                       # 中轨死区
        score = 0.0
        signal = "hold"
        confidence = 0.4
        detail = f"中轨附近(位置{position:.0%}),观望"

    return signal, round(score, 1), round(confidence, 2), detail


@register
class DailyBollingerOperator(BaseOperator):
    """日线布林带均值回归:中下轨买、中上轨卖。

    参数:
      window:  SMA 窗口(默认20≈1个月交易日)
      std_dev: 标准差倍数(默认2.0,标准布林带)
      buy_max: 中下轨买入阈值上界(默认0.45,position<此值=买)
      sell_min: 中上轨卖出阈值下界(默认0.55,position>此值=卖)
                buy_max..sell_min 之间为中轨死区(观望)。
    """

    operator_id = "daily_bollinger"
    type = "math"
    PARAMS_SCHEMA = {"window": 20, "std_dev": 2.0, "buy_max": 0.45, "sell_min": 0.55}

    def run(self, ctx, params):
        window = int(params.get("window", 20))
        k = float(params.get("std_dev", 2.0))
        buy_max = float(params.get("buy_max", 0.45))
        sell_min = float(params.get("sell_min", 0.55))

        closes = quote_series(ctx.code, "close", window + 30, as_of=ctx.as_of)
        if len(closes) < window:
            return OpResult(
                operator=self.operator_id, type="math", value=None,
                signal="hold", score=0.0, confidence=0.3,
                reasoning=f"日线样本不足({len(closes)}<{window})",
            )

        sma, upper, lower, bandwidth = _calc_bollinger(closes, window, k)
        if sma is None:
            return OpResult(
                operator=self.operator_id, type="math", value=None,
                signal="hold", score=0.0, confidence=0.3,
                reasoning="日线布林带计算失败",
            )

        latest_close = closes[-1]
        signal, score, confidence, detail = _position_score(
            latest_close, upper, lower, buy_max, sell_min)

        band_note = ""
        if bandwidth < 5:
            band_note = "带宽窄,变盘前兆"
        elif bandwidth > 20:
            band_note = "带宽宽,趋势加速"

        reasoning = (
            f"[日线BOLL w={window} k={k} buy<{buy_max} sell>{sell_min}] {detail}. "
            f"上轨={upper:.2f} 中轨={sma:.2f} 下轨={lower:.2f} "
            f"现价={latest_close:.2f} 带宽={bandwidth:.1f}%"
        )
        if band_note:
            reasoning += f" ({band_note})"

        return OpResult(
            operator=self.operator_id, type="math",
            signal=signal, score=score, confidence=confidence,
            value=round((latest_close - lower) / ((upper - lower) or 1.0), 3),
            reasoning=reasoning,
        )
