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

from datetime import date, timedelta
import math

from sqlmodel import select

from stockfu.ai.operators.base import BaseOperator, OpResult
from stockfu.ai.operators.registry import register
from stockfu.db import session_scope
from stockfu.services.factors import quote_model_for


def _weekly_series_from_rows(rows):
    """从日线查询结果中提取: 日线收盘价序列 + 周线收盘价序列。

    周聚合: 按 ISO 周,取每周最后一个交易日收盘价。
    返回 (daily_closes, weekly_closes)。
    """
    daily_closes: list[float] = []
    weekly: dict[tuple[int, int], float] = {}  # (year, week) -> close

    for r in rows:
        d = getattr(r, "quote_date", None) or getattr(r, "snap_date")
        close = getattr(r, "close", None)
        if close is not None and float(close) > 0:
            daily_closes.append(float(close))
            iso = d.isocalendar()
            weekly[(iso[0], iso[1])] = float(close)  # 后面的覆盖前面的 -> 每周最后一天

    weekly_closes = [weekly[k] for k in sorted(weekly.keys())]
    return daily_closes, weekly_closes


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

    buy_max/sell_min 参数化位置阈值(默认 0.3/0.7 = 旧行为,逐字节一致):
      position < buy_max  → 买(下轨附近);position > sell_min → 卖(上轨附近);
      buy_max..sell_min   → 中轨观望。
    返回 (signal, score, raw, confidence, detail);score=clamp(raw,±20),raw 供截面排名。
    """
    band_range = upper - lower if upper > lower else 1.0
    position = (latest_close - lower) / band_range

    if latest_close >= upper:
        exceed = (latest_close - upper) / band_range
        raw = -8.0 - exceed * 15.0
        score = max(-20.0, raw)
        signal = "sell" if exceed < 0.5 else "strong_sell"
        confidence = min(0.8, 0.5 + abs(score) / 40.0)
        detail = f"周线突破上轨(超出{exceed:.1%}),周线级别超买"
    elif latest_close <= lower:
        exceed = (lower - latest_close) / band_range
        raw = 8.0 + exceed * 15.0
        score = min(20.0, raw)
        signal = "buy" if exceed < 0.5 else "strong_buy"
        confidence = min(0.8, 0.5 + abs(score) / 40.0)
        detail = f"周线跌破下轨(超出{exceed:.1%}),周线级别超卖"
    elif position < buy_max:
        raw = 6.0 * (1.0 - position / buy_max)
        score = raw
        signal = "buy"
        confidence = 0.55
        detail = f"周线下轨附近(位置{position:.0%}<{buy_max:.0%}),支撑区"
    elif position > sell_min:
        raw = -6.0 * ((position - sell_min) / (1.0 - sell_min))
        score = raw
        signal = "sell"
        confidence = 0.55
        detail = f"周线上轨附近(位置{position:.0%}>{sell_min:.0%}),压力区"
    else:
        raw = 0.0
        score = 0.0
        signal = "hold"
        confidence = 0.4
        detail = f"周线中轨附近(位置{position:.0%}),方向不明"

    return signal, round(score, 1), round(raw, 2), round(confidence, 2), detail


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
        ref = ctx.as_of or date.today()

        # 需要约 window*7 + 60 个交易日才能凑够 window 周的日线
        lookback_days = window * 7 + 60
        start = ref - timedelta(days=lookback_days)
        model = quote_model_for(ctx.code)

        with session_scope() as s:
            rows = s.exec(
                select(model).where(
                    model.asset_code == ctx.code,
                    model.quote_date >= start,
                    model.quote_date <= ref,
                ).order_by(model.quote_date)
            ).all()

        if not rows:
            return OpResult(
                operator=self.operator_id, type="math", value=None,
                signal="hold", score=0.0, confidence=0.0,
                reasoning=f"{ctx.code} 无日线数据",
            )

        daily_closes, weekly_closes = _weekly_series_from_rows(rows)

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
        signal, score, raw, confidence, pos_detail = _position_score(
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
            signal=signal, score=score, raw_score=raw, confidence=confidence,
            value=round((latest_close - lower) / ((upper - lower) or 1.0), 3),
            reasoning=reasoning,
            evidence={
                "sma": round(sma, 2),
                "upper": round(upper, 2),
                "lower": round(lower, 2),
                "bandwidth": round(bandwidth, 1),
                "position_pct": round(
                    (latest_close - lower) / ((upper - lower) or 1.0) * 100, 1
                ),
                "weekly_count": len(weekly_closes),
                "daily_count": len(daily_closes),
                "window": window,
                "std_dev": k,
            },
        )
