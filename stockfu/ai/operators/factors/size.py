"""规模 / 小市值算子: 总市值 → score(±20)。小市值 → 正分(小盘异象)。

Banz(1981)小盘效应 + A 股「小盘主线」(2025-2026 延续,广发/中信等卖方一致预期):
小市值票长期超额(覆盖度广、机构低配、壳价值)。log10 映射——30 亿(log9.5)→ +20,
3000 亿(log11.5)→ 0, 1 万亿+(log12.5)→ −10。rebalancer 横截面排序选小盘。

**数据口径**:优先用 quote_snapshot.market_cap;但当前 baostock 回补未抓 mktcap 字段
(全库 market_cap 为空),故提供**派生代理**:market_cap ≈ amount×100/turnover
(turnover=volume/总股本×100,amount≈volume×均价 → amount×100/turnover≈总股本×价=市值)。
取近 window 日代理均值(结构量,逐日稳定)。market_cap 真正回补后自动用真值。

**注**:A 股小盘超额近年受微盘流动性风险扰动(2024 初微盘崩跌),实务需结合低波 / 低换手
过滤流动性陷阱。
"""
import math

from stockfu.ai.operators.base import BaseOperator, OpResult
from stockfu.ai.operators.registry import register
from stockfu.services.factors import quote_series


@register
class SizeOperator(BaseOperator):
    operator_id = "size"
    type = "math"
    PARAMS_SCHEMA = {"window": 20}   # 代理均值窗(market_cap 真值时取末值)

    def run(self, ctx, params):
        window = int(params.get("window", 20))
        # 优先真值 market_cap;空则派生代理
        mcaps = [m for m in quote_series(ctx.code, "market_cap", 10, as_of=ctx.as_of)
                 if m is not None and m > 0]
        if mcaps:
            mcap = float(mcaps[-1])
            src = "market_cap"
        else:
            amts = quote_series(ctx.code, "amount", window + 30, as_of=ctx.as_of)
            turns = quote_series(ctx.code, "turnover", window + 30, as_of=ctx.as_of)
            n = min(len(amts), len(turns))
            prox = [amts[i] * 100.0 / turns[i] for i in range(n - window, n)
                    if i >= 0 and amts[i] and turns[i] and turns[i] > 0]
            if len(prox) < max(window // 2, 5):
                return OpResult(operator=self.operator_id, type="math", value=None,
                                signal="hold", score=0.0, confidence=0.3,
                                reasoning="无市值数据(market_cap 未回补且 amount/turnover 样本不足)")
            mcap = sum(prox) / len(prox)
            src = f"代理(amount×100/turnover,{len(prox)}日均)"
        if mcap <= 0:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning="市值无效(≤0)")
        logm = math.log10(mcap)                        # 1e9→9, 1e11→11, 1e12→12
        score = max(-10.0, min(20.0, (11.5 - logm) / 2.0 * 20.0))   # log9.5→+20,11.5→0,12.5→−10
        signal = "buy" if score > 5 else "sell" if score < -5 else "hold"
        yi = mcap / 1e8                                # 亿元
        return OpResult(operator=self.operator_id, type="math", value=round(yi, 1),
                        signal=signal, score=round(score, 1), confidence=0.55 if "代理" in src else 0.6,
                        reasoning=f"总市值 {yi:.0f} 亿(log10={logm:.2f},{src})")
