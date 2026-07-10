"""volatility: 波动率状态分析(ATR + 分位)(operators/llm 镜像,逐字复制自 skills/tools/volatility.py)"""
from stockfu.services.factors import quote_series, percentile
import math

SCHEMA = {
    "type": "function",
    "function": {
        "name": "volatility",
        "description": "当前波动率状态与历史分位。ATR=平均真实波幅,波动率分位高(>80)=异常波动(风险),分位低(<20)=极度平静(变盘前兆),正常=中位区间",
        "parameters": {
            "type": "object",
            "properties": {
                "period": {"type": "integer", "description": "ATR计算周期,默认14"},
            },
        },
    },
}
USED_BY = {"risk"}
REQUIRED_FIELDS = ["close", "high", "low"]


def execute(code: str, period: int = 14, as_of=None) -> str:
    closes = quote_series(code, "close", period * 4 + 10, as_of=as_of)
    highs = quote_series(code, "high", period * 4 + 10, as_of=as_of)
    lows = quote_series(code, "low", period * 4 + 10, as_of=as_of)

    if len(closes) < period + 1 or len(highs) < period + 1 or len(lows) < period + 1:
        return f"数据不足:需至少{period + 1}个交易日(close={len(closes)})"

    # Calculate ATR
    trs: list[float] = []
    for i in range(1, min(len(highs), len(lows), len(closes))):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i - 1]) if i - 1 >= 0 else 0
        lc = abs(lows[i] - closes[i - 1]) if i - 1 >= 0 else 0
        trs.append(max(hl, hc, lc))

    if len(trs) < period:
        return "数据不足:无法计算ATR"

    # Simple ATR (SMA)
    atr = sum(trs[-period:]) / period
    latest_close = closes[-1]
    atr_pct = atr / latest_close * 100 if latest_close > 0 else 0

    # Volatility percentile (using ATR values as series)
    vol_pct, sample = percentile(trs, trs[-1])

    parts = [f"ATR({period})={atr:.2f}, 约占现价{atr_pct:.1f}%"]

    if vol_pct is not None:
        if vol_pct >= 80:
            parts.append(f"⚠️ 波动率处于历史高分位({vol_pct:.0f}%),异常波动期,注意风险")
        elif vol_pct <= 20:
            parts.append(f"波动率处于历史低分位({vol_pct:.0f}%),极度平静期,变盘概率上升")
        else:
            parts.append(f"波动率正常(历史{vol_pct:.0f}%分位)")
    else:
        parts.append(f"波动率样本不足(当前{len(trs)})")

    parts.append(f"ATR(价差比)={atr_pct:.1f}%: {'高波动' if atr_pct > 4 else '正常' if atr_pct > 2 else '低波动'}")
    return " | ".join(parts)
