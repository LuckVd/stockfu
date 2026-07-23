"""周线布林带算子: 周线 Bollinger Bands 位置作为因子。

核心逻辑:
  1. 取日线收盘价 → 按周聚合(每周最后一个交易日 close)
  2. 在周线上算 SMA(window) ± k×Std(window)
  3. 当前最新日线收盘价在周线布林带中的位置 → 多空信号

无未来函数: as_of 限制了数据上界。

与月线布林带对比:
  - 月线布林带: 20月≈1.7年,适合大周期方向判断
  - 周线布林带: 20周≈5个月,适合中周期择时,反应更快
"""

import math

from stockfu.ai.operators.base import BaseOperator, OpResult
from stockfu.ai.operators.registry import register
from stockfu.services.factors import quote_series_dates


def _weekly_series_from_pairs(pairs) -> list[float]:
    """(date, close) 升序对 → 周度收盘价序列(每周最后交易日 close,后覆盖前)。

    周聚合与原 rows 版逐值一致:按 ISO 周,同周后写覆盖前写 → 取该周最后交易日。
    """
    weekly: dict[tuple[int, int], float] = {}  # (iso_year, iso_week) -> close
    for d, c in pairs:
        if c > 0:
            iso = d.isocalendar()
            weekly[(iso[0], iso[1])] = c
    return [weekly[k] for k in sorted(weekly.keys())]


def _weekly_series_from_rows(rows) -> tuple[list[float], list[float]]:
    """兼容行业轮动探针：ORM 行 → 日线与周线收盘价。

    正式算子回测路径应使用 `_weekly_series_from_pairs`，以便从预载行情取数；
    该纯转换包装保留给探针等已有调用方，不开数据库连接。
    """
    pairs = []
    for r in rows:
        d = getattr(r, "quote_date", None) or getattr(r, "snap_date", None)
        close = getattr(r, "close", None)
        if d is not None and close is not None and float(close) > 0:
            pairs.append((d, float(close)))
    return [c for _d, c in pairs], _weekly_series_from_pairs(pairs)


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


def _position_score(latest_close: float, upper: float, lower: float,
                    buy_max: float = 0.3, sell_min: float = 0.7):
    """根据价格在布林带中的位置生成信号(中下轨买 / 中上轨卖)。

    buy_max/sell_min 参数化位置阈值(默认 0.3/0.7):
      position < buy_max  → 买(下轨附近);position > sell_min → 卖(上轨附近);
      buy_max..sell_min   → 中轨观望。
    返回 (signal, score, confidence, detail);score=连续强度(不 clamp)。
    """
    band_range = upper - lower if upper > lower else 1.0
    position = (latest_close - lower) / band_range

    if latest_close >= upper:
        exceed = (latest_close - upper) / band_range
        score = -8.0 - exceed * 15.0
        signal = "sell" if exceed < 0.5 else "strong_sell"
        confidence = min(0.8, 0.5 + abs(score) / 40.0)
        detail = f"周线突破上轨(超出{exceed:.1%}),周线级别超买"
    elif latest_close <= lower:
        exceed = (lower - latest_close) / band_range
        score = 8.0 + exceed * 15.0
        signal = "buy" if exceed < 0.5 else "strong_buy"
        confidence = min(0.8, 0.5 + abs(score) / 40.0)
        detail = f"周线跌破下轨(超出{exceed:.1%}),周线级别超卖"
    elif position < buy_max:
        score = 6.0 * (1.0 - position / buy_max)
        signal = "buy"
        confidence = 0.55
        detail = f"周线下轨附近(位置{position:.0%}<{buy_max:.0%}),支撑区"
    elif position > sell_min:
        score = -6.0 * ((position - sell_min) / (1.0 - sell_min))
        signal = "sell"
        confidence = 0.55
        detail = f"周线上轨附近(位置{position:.0%}>{sell_min:.0%}),压力区"
    else:
        score = 0.0
        signal = "hold"
        confidence = 0.4
        detail = f"周线中轨附近(位置{position:.0%}),方向不明"

    return signal, round(score, 1), round(confidence, 2), detail


@register
class WeeklyBollingerOperator(BaseOperator):
    """周线布林带: 基于周线周期的布林轨道位置判断。

    适用于:
      - 中周期择时(比月线灵敏,比日线稳定)
      - 趋势加速/反转判断
      - 和月线布林带配合做多周期确认

    参数:
      window: 周线窗口(默认20≈5个月)
      std_dev: 标准差倍数(默认2.0)
    """
    operator_id = "weekly_bollinger"
    type = "math"
    PARAMS_SCHEMA = {"window": 20, "std_dev": 2.0, "buy_max": 0.3, "sell_min": 0.7}

    def run(self, ctx, params):
        window = int(params.get("window", 20))
        k = float(params.get("std_dev", 2.0))
        buy_max = float(params.get("buy_max", 0.3))
        sell_min = float(params.get("sell_min", 0.7))
        # 需约 window*7 + 60 个交易日才能凑够 window 周的日线;
        # 走 quote_series_dates → 回测时从预载内存切片(零 DB),不再逐 (code,as_of) 开 session
        lookback_days = window * 7 + 60

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
        weekly_closes = _weekly_series_from_pairs(pairs)

        if len(daily_closes) < 5:
            return OpResult(
                operator=self.operator_id, type="math", value=None,
                signal="hold", score=0.0, confidence=0.0,
                reasoning="日线数据不足5个交易日",
            )

        if len(weekly_closes) < window:
            return OpResult(
                operator=self.operator_id, type="math", value=None,
                signal="hold", score=0.0, confidence=0.3,
                reasoning=(
                    f"周线数据不足: {len(weekly_closes)}周,"
                    f"需至少{window}周(window={window})"
                ),
            )

        sma, upper, lower, bandwidth = _calc_bollinger(weekly_closes, window, k)
        if sma is None:
            return OpResult(
                operator=self.operator_id, type="math", value=None,
                signal="hold", score=0.0, confidence=0.3,
                reasoning="周线布林带计算失败",
            )

        latest_close = daily_closes[-1]
        signal, score, confidence, pos_detail = _position_score(
            latest_close, upper, lower, buy_max, sell_min,
        )

        band_note = ""
        if bandwidth < 5:
            band_note = "带宽窄,变盘前兆"
        elif bandwidth > 20:
            band_note = "带宽宽,趋势加速"

        reasoning = (
            f"[周线BOLL w={window} k={k}] {pos_detail}. "
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
