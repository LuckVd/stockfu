"""macd: MACD 金叉/死叉/柱线/零轴位置/顶底背离"""
from stockfu.services.factors import quote_series


def _ema(data: list[float], period: int) -> list[float | None]:
    if len(data) < period:
        return [None] * len(data)
    alpha = 2 / (period + 1)
    ema_vals: list[float] = [sum(data[:period]) / period]
    for i in range(period, len(data)):
        ema_vals.append(ema_vals[-1] * (1 - alpha) + data[i] * alpha)
    return [None] * (period - 1) + ema_vals


SCHEMA = {
    "type": "function",
    "function": {
        "name": "macd",
        "description": "MACD 快慢线金叉/死叉、零轴位置、柱线放缩、顶底背离判断。金叉=买入信号,死叉=卖出信号,柱线放大=趋势加速,缩小=趋势衰减",
        "parameters": {
            "type": "object",
            "properties": {
                "fast": {"type": "integer", "description": "快线EMA周期,默认12"},
                "slow": {"type": "integer", "description": "慢线EMA周期,默认26"},
                "signal": {"type": "integer", "description": "信号线EMA周期,默认9"},
            },
        },
    },
}
USED_BY = {"trend", "contrarian", "risk"}
REQUIRED_FIELDS = ["close"]


def execute(code: str, fast: int = 12, slow: int = 26, signal: int = 9) -> str:
    need = slow + signal + 30
    closes = quote_series(code, "close", need)
    if len(closes) < slow + signal:
        return f"数据不足:需至少{slow + signal}个交易日(当前{len(closes)})"

    ema_f = _ema(closes, fast)
    ema_s = _ema(closes, slow)
    dif = [f - s if f is not None and s is not None else None for f, s in zip(ema_f, ema_s)]
    dif_filtered = [x for x in dif if x is not None]
    if len(dif_filtered) < signal:
        return "MACD 数据不足"

    dea = _ema(dif_filtered, signal)
    # Align lengths
    pad = len(dif) - len(dif_filtered)
    dea_padded: list[float | None] = [None] * (pad + len(dif_filtered) - len(dea)) + dea + [None] * (len(dif) - pad - len(dif_filtered))

    # Latest values
    dif_v = dif[-1]
    dea_v = dea_padded[-1]
    if dif_v is None or dea_v is None:
        return "MACD 最新值缺失"

    hist = dif_v - dea_v
    # Cross
    prev_dif = dif[-2] if len(dif) >= 2 and dif[-2] is not None else dif_v
    prev_dea = dea_padded[-2] if len(dea_padded) >= 2 and dea_padded[-2] is not None else dea_v
    cross_up = prev_dif <= prev_dea and dif_v > dea_v
    cross_down = prev_dif >= prev_dea and dif_v < dea_v

    # Hist trend
    prev_hist = (dif[-2] - dea_padded[-2]) if len(dif) >= 2 and dif[-2] is not None and dea_padded[-2] is not None else hist
    hist_trend = "柱线放大" if abs(hist) > abs(prev_hist) else "柱线缩小"

    pos = "零轴上方" if dif_v > 0 else "零轴下方"

    parts = [f"DIF={dif_v:.2f}, DEA={dea_v:.2f}, 柱={hist:+.4f}", f"快慢线在{pos}"]
    if cross_up:
        parts.append("⚠️ 金叉(买入信号)")
    elif cross_down:
        parts.append("⚠️ 死叉(卖出信号)")
    else:
        parts.append("无交叉")
    parts.append(hist_trend)

    return " | ".join(parts)
