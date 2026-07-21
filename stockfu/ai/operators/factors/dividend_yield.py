"""股息率算子: TTM 每股现金分红 / **不复权**股价 → score(±20)。高股息→正分。

红利因子在 A 股长期有效(尤其熊市防御、震荡市现金流回报)。TTM 口径(近 365 天
每股现金分红),分红来自 dividend_event 表。
**分母 close_raw(不复权)**:禁止用前复权价,否则名义现金/qfq 虚高并引入未来分红前视。
用绝对股息率映射(非历史分位):≥ high_yield 满分 +20,1%~high 线性,<1% 零——
配合 rebalancer 横截面排序选最高息个股。

price_basis=raw 写入 params 指纹 → 改口径后旧 operator_result 自动失效。
"""
from stockfu.ai.operators.base import BaseOperator, OpResult
from stockfu.ai.operators.registry import register
from stockfu.services.dividend import dividend_yield_ttm


@register
class DividendYieldOperator(BaseOperator):
    operator_id = "dividend_yield"
    type = "math"
    # price_basis=raw:分母不复权;进指纹,切换口径自动废缓存
    PARAMS_SCHEMA = {"high_yield": 5.0, "price_basis": "raw", "yield_cap": 20.0}

    def run(self, ctx, params):
        high = float(params.get("high_yield", 5.0))
        cap = float(params.get("yield_cap", 20.0))
        res = dividend_yield_ttm(ctx.code, as_of=ctx.as_of)
        if res is None:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning="无 TTM 分红或无 close_raw(未回补不复权价?)")
        y, ttm = res
        if cap > 0:
            y = min(y, cap)
        if y >= high:
            score = 20.0
            signal = "buy"
            reasoning = f"TTM 股息率 {y:.2f}% 高息(≥{high}%,分母raw)"
        elif y >= 1.0:
            score = min(20 * (y - 1) / (high - 1), 20.0)   # 1%→0, high→20
            signal = "buy" if y >= 3.0 else "hold"
            reasoning = f"TTM 股息率 {y:.2f}%(每股分红 {ttm} 元,raw)"
        else:
            score = 0.0
            signal = "hold"
            reasoning = f"TTM 股息率 {y:.2f}% 偏低(raw)"
        return OpResult(operator=self.operator_id, type="math", value=round(y, 2),
                        signal=signal, score=round(score, 1), confidence=0.6,
                        reasoning=reasoning)
