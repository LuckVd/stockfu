"""月线布林带算子: 月线 Bollinger Bands 位置作为因子。

核心逻辑:
  1. 取日线收盘价 → 按月聚合(每月最后交易日 close)
  2. 在月线上算 SMA(window) ± k×Std(window)
  3. 当前最新日线收盘价在月线布林带中的位置 → 多空信号

无未来函数:
  as_of 限制了数据上界,所有计算只用 <=as_of 的数据。
  月线周期消除了日线噪音,信号比日线布林带更稳定,适合中长期择时。

与日线布林带对比:
  - 日线布林带: 灵敏但噪音多,适合短线(20日≈1个月)
  - 月线布林带: 平滑但滞后,适合趋势/反转判断(20月≈1.7年周期)
"""

from datetime import date
import math

from stockfu.ai.operators.base import BaseOperator, OpResult
from stockfu.ai.operators.registry import register
from stockfu.services.factors import quote_series_dates


def _monthly_series_from_pairs(pairs) -> list[float]:
    """(date, close) 升序对 → 月度收盘价序列(每月最后交易日 close,后覆盖前)。

    月度聚合与原 rows 版逐值一致:pairs 已按日期升序,同月后写覆盖前写 → 取该月最后交易日。
    """
    monthly: dict[tuple[int, int], float] = {}
    for d, c in pairs:
        if c > 0:
            monthly[(d.year, d.month)] = c
    return [monthly[k] for k in sorted(monthly.keys())]


def _calc_bollinger(series: list[float], window: int, k: float):
    """计算布林带,返回最新的 (sma, upper, lower, bandwidth)。"""
    if len(series) < window:
        return None, None, None, None
    recent = series[-window:]
    sma = sum(recent) / window
    variance = sum((x - sma) ** 2 for x in recent) / window
    std = math.sqrt(variance)
    upper = sma + k * std
    lower = sma - k * std
    bandwidth = (upper - lower) / sma * 100 if sma > 0 else 0.0
    return sma, upper, lower, bandwidth


def _position_score(
    latest_close: float, upper: float, lower: float,
) -> tuple[str, float, float, str]:
    """根据价格在布林带中的位置生成信号。

    返回 (signal, score, confidence, reasoning)。score=连续强度(不 clamp)。
    """
    band_range = upper - lower if upper > lower else 1.0
    position = (latest_close - lower) / band_range  # 0=下轨 1=上轨

    if latest_close >= upper:
        exceed = (latest_close - upper) / band_range
        score = -8.0 - exceed * 15.0
        signal = "sell" if exceed < 0.5 else "strong_sell"
        confidence = min(0.8, 0.5 + abs(score) / 40.0)
        detail = f"月线突破上轨(超出{exceed:.1%}),月线级别超买"
    elif latest_close <= lower:
        exceed = (lower - latest_close) / band_range
        score = 8.0 + exceed * 15.0
        signal = "buy" if exceed < 0.5 else "strong_buy"
        confidence = min(0.8, 0.5 + abs(score) / 40.0)
        detail = f"月线跌破下轨(超出{exceed:.1%}),月线级别超卖"
    elif position < 0.3:
        score = 6.0 * (1.0 - position / 0.3)
        signal = "buy"
        confidence = 0.55
        detail = f"月线下轨附近(位置{position:.0%}),估值支撑区"
    elif position > 0.7:
        score = -6.0 * ((position - 0.7) / 0.3)
        signal = "sell"
        confidence = 0.55
        detail = f"月线上轨附近(位置{position:.0%}),估值压力区"
    else:
        score = 0.0
        signal = "hold"
        confidence = 0.4
        detail = f"月线中轨附近(位置{position:.0%}),方向不明"

    return signal, round(score, 1), round(confidence, 2), detail


@register
class MonthlyBollingerOperator(BaseOperator):
    """月线布林带: 基于月线周期的布林轨道位置判断。

    适用于:
      - 月线触及/突破上下轨 → 反转信号(均值回归)
      - 带宽极度收缩 → 变盘前兆(配合其他算子使用)
      - 月线级别趋势加速判断

    参数:
      window: 月线窗口(默认20≈1.7年,覆盖A股一个牛熊周期)
      std_dev: 标准差倍数(默认2.0,标准布林带)
    """
    operator_id = "monthly_bollinger"
    type = "math"
    PARAMS_SCHEMA = {"window": 20, "std_dev": 2.0}

    def run(self, ctx, params):
        window = int(params.get("window", 20))
        k = float(params.get("std_dev", 2.0))
        # 需约 window*31 + 120 个交易日才能凑够 window 个月的日线;
        # 走 quote_series_dates → 回测时从预载内存切片(零 DB),不再逐 (code,as_of) 开 session
        lookback_days = window * 31 + 120

        dates, closes = quote_series_dates(
            ctx.code, "close", lookback_days, as_of=ctx.as_of)
        pairs = [(d, c) for d, c in zip(dates, closes) if c > 0]

        if not pairs:
            return OpResult(
                operator=self.operator_id, type="math", value=None,
                signal="hold", score=0.0, confidence=0.0,
                reasoning=f"{ctx.code} 无日线数据",
            )

        daily_closes = [c for _, c in pairs]
        monthly_closes = _monthly_series_from_pairs(pairs)

        if len(daily_closes) < 5:
            return OpResult(
                operator=self.operator_id, type="math", value=None,
                signal="hold", score=0.0, confidence=0.0,
                reasoning="日线数据不足5个交易日",
            )

        if len(monthly_closes) < window:
            return OpResult(
                operator=self.operator_id, type="math", value=None,
                signal="hold", score=0.0, confidence=0.3,
                reasoning=(
                    f"月线数据不足: {len(monthly_closes)}个月,"
                    f"需至少{window}个月(window={window})"
                ),
            )

        # 计算月线布林带
        sma, upper, lower, bandwidth = _calc_bollinger(monthly_closes, window, k)
        if sma is None:
            return OpResult(
                operator=self.operator_id, type="math", value=None,
                signal="hold", score=0.0, confidence=0.3,
                reasoning="月线布林带计算失败",
            )

        latest_close = daily_closes[-1]
        signal, score, confidence, pos_detail = _position_score(
            latest_close, upper, lower,
        )

        # 带宽辅助判断
        band_note = ""
        if bandwidth < 8:
            band_note = "带宽窄,变盘前兆"
        elif bandwidth > 25:
            band_note = "带宽宽,趋势加速"

        reasoning = (
            f"[月线BOLL w={window} k={k}] {pos_detail}. "
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
