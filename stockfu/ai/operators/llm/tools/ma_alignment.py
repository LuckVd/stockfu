"""ma_alignment: 短中长期均线排列判断(operators/llm 镜像,逐字复制自 skills/tools/ma_alignment.py)"""
from stockfu.services.factors import quote_series

SCHEMA = {
    "type": "function",
    "function": {
        "name": "ma_alignment",
        "description": "判断 MA5/MA10/MA20 均线处于多头排列(=趋势看涨)、空头排列(=趋势看跌)还是交叉/无序(=无明确方向)",
        "parameters": {
            "type": "object",
            "properties": {
                "short_ma": {"type": "integer", "description": "短期均线天数,默认5"},
                "mid_ma": {"type": "integer", "description": "中期均线天数,默认10"},
                "long_ma": {"type": "integer", "description": "长期均线天数,默认20"},
            },
        },
    },
}
USED_BY = {"trend", "risk"}
REQUIRED_FIELDS = ["close"]


def execute(code: str, short_ma: int = 5, mid_ma: int = 10, long_ma: int = 20, as_of=None) -> str:
    closes = quote_series(code, "close", long_ma + 10, as_of=as_of)
    if len(closes) < long_ma:
        return f"数据不足:需至少{long_ma}个交易日(当前{len(closes)})"
    ma_s = sum(closes[-short_ma:]) / short_ma
    ma_m = sum(closes[-mid_ma:]) / mid_ma
    ma_l = sum(closes[-long_ma:]) / long_ma

    if ma_s > ma_m > ma_l:
        direction = "多头排列,均线向上发散"
    elif ma_s < ma_m < ma_l:
        direction = "空头排列,均线向下发散"
    else:
        direction = "均线交叉/无序,无明确方向"

    return f"MA{short_ma}={ma_s:.2f}, MA{mid_ma}={ma_m:.2f}, MA{long_ma}={ma_l:.2f} → {direction}"
