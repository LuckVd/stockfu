"""bollinger: 布林带位置判断(operators/llm 镜像,逐字复制自 skills/tools/bollinger.py)"""
from stockfu.services.factors import quote_series
import math

SCHEMA = {
    "type": "function",
    "function": {
        "name": "bollinger",
        "description": "价格在布林轨道中的位置。触上轨=超买,触下轨=超卖,带宽收缩=变盘前兆,带宽扩张=趋势加速",
        "parameters": {
            "type": "object",
            "properties": {
                "period": {"type": "integer", "description": "中轨MA周期,默认20"},
                "std_dev": {"type": "number", "description": "标准差倍数,默认2.0"},
            },
        },
    },
}
USED_BY = {"trend", "contrarian", "risk"}
REQUIRED_FIELDS = ["close"]


def execute(code: str, period: int = 20, std_dev: float = 2.0, as_of=None) -> str:
    closes = quote_series(code, "close", period * 3 + 10, as_of=as_of)
    if len(closes) < period:
        return f"数据不足:需至少{period}个交易日(当前{len(closes)})"

    ma = sum(closes[-period:]) / period
    variance = sum((c - ma) ** 2 for c in closes[-period:]) / period
    sd = math.sqrt(variance) if variance > 0 else 0
    upper = ma + sd * std_dev
    lower = ma - sd * std_dev
    latest = closes[-1]
    width = (upper - lower) / ma * 100  # 带宽百分比

    # 位置
    if latest >= upper * 0.99:
        position = "已触及或接近上轨(超买)"
    elif latest <= lower * 1.01:
        position = "已触及或接近下轨(超卖)"
    elif latest > ma:
        position = "中轨与上轨之间"
    else:
        position = "中轨与下轨之间"

    # 带宽
    if width < 5:
        band = "带宽极度收缩,变盘概率高"
    elif width < 12:
        band = "带宽正常"
    else:
        band = "带宽偏大,趋势加速"

    return f"上轨={upper:.2f}, 中轨={ma:.2f}, 下轨={lower:.2f}, 现价={latest:.2f} | 价格{position} | {band}(带宽={width:.1f}%)"
