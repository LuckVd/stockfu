"""低贝塔算子: 个股相对沪深300 的 β → score(±20)。低 β → 正分(防御 / 低波动异象)。

Black 低波动异象 / Frazzini-Pedersen BAB:低 β 票风险调整后长期占优(高 β 票被过度追捧、
回撤深)。β = cov(stock, bench) / var(bench)(window 日日收益)。A 股防御因子 2025 走强。
基准 sh000300(沪深300);benchmark 序列按 as_of 进程内缓存(所有 code 共享,回测内每日
仅 1 次 DB,避免 N+1)。**按日期交集对齐** stock/bench(避免长度不等时末段截断错配)。
低 β 看多:β 0.5→+20, 1.0→0, 1.5→−20(线性)。
"""
from datetime import date

from stockfu.ai.operators.base import BaseOperator, OpResult
from stockfu.ai.operators.registry import register
from stockfu.services.factors import quote_series_dates

_BENCH = "sh000300"
_BENCH_CACHE: dict[tuple[date, int], tuple[list, list]] = {}


def _bench_series(as_of, length: int):
    """沪深300 (dates, closes)(按 (as_of, length) 缓存;回测内每日仅 1 次 DB)。"""
    key = (as_of or date.today(), length)
    cached = _BENCH_CACHE.get(key)
    if cached is not None:
        return cached
    pair = quote_series_dates(_BENCH, "close", length, as_of=as_of)
    _BENCH_CACHE[key] = pair
    if len(_BENCH_CACHE) > 64:                        # 上限保护(防跨多 as_of 膨胀)
        _BENCH_CACHE.pop(next(iter(_BENCH_CACHE)))
    return pair


@register
class LowBetaOperator(BaseOperator):
    operator_id = "low_beta"
    type = "math"
    PARAMS_SCHEMA = {"window": 120, "bench": "sh000300"}

    def run(self, ctx, params):
        window = int(params.get("window", 120))
        span = int(window * 1.5) + 30                  # 日历日缓冲(120 交易日≈180 日历日)
        s_dates, sc = quote_series_dates(ctx.code, "close", span, as_of=ctx.as_of)
        b_dates, bc = _bench_series(ctx.as_of, span)
        # 按日期对齐(交集):避免长度不等时末段截断错配 → β 失真
        smap = {d: v for d, v in zip(s_dates, sc) if v is not None}
        bmap = {d: v for d, v in zip(b_dates, bc) if v is not None}
        common = sorted(set(smap) & set(bmap))
        if len(common) < 21:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning=f"低 β 共同样本不足({len(common)})")
        common = common[-(window + 1):]                # 取末 window+1 个共同日
        sv = [smap[d] for d in common]
        bv = [bmap[d] for d in common]
        sr = [sv[i] / sv[i - 1] - 1 for i in range(1, len(sv))]
        br = [bv[i] / bv[i - 1] - 1 for i in range(1, len(bv))]
        m = len(sr)
        sm = sum(sr) / m
        bm = sum(br) / m
        cov = sum((sr[i] - sm) * (br[i] - bm) for i in range(m)) / m
        var = sum((b - bm) ** 2 for b in br) / m
        if var <= 0:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning="基准方差为 0")
        beta = cov / var
        score = max(-20.0, min(20.0, 40.0 * (1.0 - beta)))   # β 0.5→+20,1.0→0,1.5→−20
        signal = "buy" if beta < 0.8 else "sell" if beta > 1.2 else "hold"
        return OpResult(operator=self.operator_id, type="math", value=round(beta, 3),
                        signal=signal, score=round(score, 1), confidence=0.6,
                        reasoning=f"β={beta:.2f}(相对沪深300,{len(common)} 日)")
