"""rsi: RSI 超买/超卖判断"""
from stockfu.services.factors import quote_series

SCHEMA = {
    "type": "function",
    "function": {
        "name": "rsi",
        "description": "RSI 数值及超买/超卖判断。RSI>70=超买(可能回调),RSI<30=超卖(可能反弹),50附近=中性",
        "parameters": {
            "type": "object",
            "properties": {
                "period": {"type": "integer", "description": "RSI 计算周期,默认14"},
            },
        },
    },
}
USED_BY = {"contrarian", "risk"}
REQUIRED_FIELDS = ["close"]


def execute(code: str, period: int = 14) -> str:
    closes = quote_series(code, "close", period * 3 + 10)
    if len(closes) < period + 1:
        return f"数据不足:需至少{period + 1}个交易日(当前{len(closes)})"

    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    avg_g = sum(gains[-period:]) / period
    avg_l = sum(losses[-period:]) / period

    if avg_l == 0:
        rsi = 100.0
    elif avg_g == 0:
        rsi = 0.0
    else:
        rs = avg_g / avg_l
        rsi = 100 - 100 / (1 + rs)

    if rsi >= 70:
        zone = "⚠️ 超买区,可能回调"
    elif rsi <= 30:
        zone = "⚠️ 超卖区,可能反弹"
    elif 40 <= rsi <= 60:
        zone = "中性区"
    else:
        zone = "偏%s(未极端)" % ("强" if rsi > 60 else "弱")

    return f"RSI({period})={rsi:.1f}, 处于{zone}"
