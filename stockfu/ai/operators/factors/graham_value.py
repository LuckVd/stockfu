"""格雷厄姆防御价值算子: PE + PB + 股息 复合 → score(±20)。低估 + 有分红 → 正分。

格雷厄姆《聪明的投资者》防御型投资者选股简化:低 PE(<15)、低 PB(<1.5)、PE×PB<22.5、
有正分红。用 PE/PB 历史分位(低估)+ 是否有 TTM 现金分红 复合打分——比单一 PE 分位更稳健的
价值复合(深低估且派息的票安全边际高)。复用 valuation_snapshot(PE/PB 分位,provider
零 DB)+ dividend_yield_ttm(分红 provider)。
"""
from datetime import date

from stockfu.ai.operators.base import BaseOperator, OpResult
from stockfu.ai.operators.registry import register
from stockfu.services.dividend import dividend_yield_ttm
from stockfu.services.valuation import valuation_snapshot


@register
class GrahamValueOperator(BaseOperator):
    operator_id = "graham_value"
    type = "math"
    PARAMS_SCHEMA = {"years": 5}

    def run(self, ctx, params):
        years = int(params.get("years", 5))
        snap = valuation_snapshot(ctx.code, ctx.as_of or date.today(), years=years)
        pe_pct = snap.get("pe_pct")
        pb_pct = snap.get("pb_pct")
        if pe_pct is None and pb_pct is None:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning="PE/PB 分位样本不足")
        # 子分:低估(分位低)→正;取 PE/PB 可用值的均值(±8)
        parts = []
        for p in (pe_pct, pb_pct):
            if p is None:
                continue
            if p < 20:
                parts.append(8.0 * (1 - p / 20))      # 分位 0→+8, 20→0
            elif p > 80:
                parts.append(-8.0 * (1 - (100 - p) / 20))
            # 20-80 中性 0
        base = sum(parts) / len(parts) if parts else 0.0
        # 股息加分:有 TTM 现金分红 → +4 上限(1.5%→+1, ≥6%→+4)
        div_bonus = 0.0
        dy_pct = 0.0
        dy = dividend_yield_ttm(ctx.code, as_of=ctx.as_of)
        if dy is not None:
            dy_pct = min(float(dy[0]), 6.0)
            div_bonus = min(4.0, dy_pct / 1.5)
        score = max(-20.0, min(20.0, base + div_bonus))
        signal = "buy" if score > 4 else "sell" if score < -4 else "hold"
        zone = snap.get("value_zone", "unknown")
        return OpResult(operator=self.operator_id, type="math",
                        value=round(pe_pct if pe_pct is not None else (pb_pct or 0), 1),
                        signal=signal, score=round(score, 1), confidence=0.6,
                        reasoning=f"格雷厄姆价值 zone={zone}(PE分位{pe_pct}/PB分位{pb_pct},息{dy_pct:.1f}%)")
