"""volume_price: 量价配合/背离分析"""
from stockfu.services.factors import quote_series

SCHEMA = {
    "type": "function",
    "function": {
        "name": "volume_price",
        "description": "分析近期量价关系:放量上涨/缩量上涨/放量下跌/缩量下跌/量价背离。缩量回调=健康,放量滞涨=危险,缩量上涨=动能不足",
        "parameters": {
            "type": "object",
            "properties": {
                "lookback": {"type": "integer", "description": "回看天数,默认20"},
                "vol_threshold": {"type": "number", "description": "量能异动倍数,默认2.0(成交量超过均量的倍数)"},
            },
        },
    },
}
USED_BY = {"trend"}
REQUIRED_FIELDS = ["close", "volume"]


def execute(code: str, lookback: int = 20, vol_threshold: float = 2.0) -> str:
    closes = quote_series(code, "close", lookback + 10)
    volumes = quote_series(code, "volume", lookback + 10)
    min_len = min(len(closes), len(volumes))
    if min_len < lookback:
        return f"数据不足:需至少{lookback}个交易日(收盘{len(closes)},量{len(volumes)})"

    # Use last `lookback` days
    c = closes[-lookback:]
    v = volumes[-lookback:]

    avg_vol = sum(v) / len(v)
    latest_vol = v[-1]
    vol_ratio = latest_vol / avg_vol if avg_vol > 0 else 1

    # Price trend
    price_start = c[0]
    price_end = c[-1]
    price_chg = (price_end / price_start - 1) * 100 if price_start > 0 else 0

    # Daily changes
    up_days = sum(1 for i in range(1, len(c)) if c[i] > c[i-1])

    parts: list[str] = [f"近{lookback}日价格变化={price_chg:+.2f}%, 上涨/下跌天数={up_days}/{lookback-up_days}"]

    # Volume vs price
    if price_chg > 3 and vol_ratio > vol_threshold:
        parts.append("放量上涨, 动能充足")
    elif price_chg > 3 and vol_ratio < 0.7:
        parts.append("缩量上涨, 动能可能不足")
    elif price_chg < -3 and vol_ratio > vol_threshold:
        parts.append("放量下跌, 卖出压力大")
    elif price_chg < -3 and vol_ratio < 0.7:
        parts.append("缩量下跌, 抛压减弱(可能企稳)")
    elif abs(price_chg) <= 3:
        if vol_ratio > vol_threshold:
            parts.append("价格窄幅波动但放量(可能有异动)")
        else:
            parts.append("量价平稳, 无异常")
    else:
        parts.append("量价关系无明显倾向")

    parts.append("最新成交量=%.0f, 为20日均量的%.1f倍" % (latest_vol, vol_ratio))
    return " | ".join(parts)
