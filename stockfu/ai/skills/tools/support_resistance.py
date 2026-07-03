"""support_resistance: 支撑位/阻力位识别"""
from stockfu.services.factors import quote_series

SCHEMA = {
    "type": "function",
    "function": {
        "name": "support_resistance",
        "description": "识别近期股价的关键支撑位和阻力位。支撑=多次下探未跌破的价格,阻力=多次上攻未突破的价格,触碰越多次越有效",
        "parameters": {
            "type": "object",
            "properties": {
                "lookback": {"type": "integer", "description": "回看天数,默认60"},
                "buckets": {"type": "integer", "description": "价格分层数,默认10"},
            },
        },
    },
}
USED_BY = {"trend", "contrarian"}
REQUIRED_FIELDS = ["close", "high", "low"]


def execute(code: str, lookback: int = 60, buckets: int = 10) -> str:
    closes = quote_series(code, "close", lookback)
    highs = quote_series(code, "high", lookback)
    lows = quote_series(code, "low", lookback)
    if len(closes) < 20 or len(highs) < 20 or len(lows) < 20:
        return f"数据不足:需至少20个交易日(close={len(closes)},high={len(highs)},low={len(lows)})"

    # Use the last `lookback` days
    c = closes[-lookback:]
    h = highs[-lookback:]
    l = lows[-lookback:]

    price_range = max(h) - min(l)
    if price_range <= 0:
        return "价格区间过小,无法识别有效的支撑阻力位"

    bucket_size = price_range / buckets
    # Count touches per bucket
    bucket_highs: dict[int, int] = {}
    bucket_lows: dict[int, int] = {}
    for i in range(len(c)):
        bh = int((h[i] - min(l)) / bucket_size) if bucket_size > 0 else 0
        bl = int((l[i] - min(l)) / bucket_size) if bucket_size > 0 else 0
        bucket_highs[bh] = bucket_highs.get(bh, 0) + 1
        bucket_lows[bl] = bucket_lows.get(bl, 0) + 1

    # Find strongest support (most-touched low bucket) and resistance (most-touched high bucket)
    sup_level = max(bucket_lows, key=bucket_lows.get) if bucket_lows else 0
    res_level = max(bucket_highs, key=bucket_highs.get) if bucket_highs else 0

    sup_price = min(l) + (sup_level + 0.5) * bucket_size
    res_price = min(l) + (res_level + 0.5) * bucket_size
    latest = c[-1]

    parts = [
        f"近{lookback}日关键阻力: {res_price:.2f}(触碰{bucket_highs.get(res_level, 0)}次)",
        f"关键支撑: {sup_price:.2f}(触碰{bucket_lows.get(sup_level, 0)}次)",
    ]

    # Current position relative to S/R
    dist_to_res = (res_price - latest) / latest * 100 if res_price > 0 else 0
    dist_to_sup = (latest - sup_price) / latest * 100 if sup_price > 0 else 0

    if dist_to_res < 3:
        parts.append(f"⚠️ 接近阻力位(距顶部仅{dist_to_res:.1f}%),注意突破/回调")
    elif dist_to_sup < 3:
        parts.append(f"⚠️ 接近支撑位(距底部仅{dist_to_sup:.1f}%),关注支撑有效性")
    else:
        parts.append(f"距离阻力{dist_to_res:.1f}%,距离支撑{dist_to_sup:.1f}%,处于中部区间")

    return " | ".join(parts)
