"""低波动算子: N 日日收益 std 的历史分位 → score(±20)。低波→正分(防御因子)。

低波动异象(low-volatility anomaly):A 股长期看波动率低的票风险调整后收益更优
(高波动票被过度投机、回撤更深)。取近 window 日日收益率标准差,在近 hist_years
年滚动 std 序列里算时序分位——衡量「当前波动相对自身历史是否异常低」,
再由 rebalancer(cap_and_rank/top_n_picker)做横截面排序选最低波个股。
"""
from stockfu.ai.operators.base import BaseOperator, OpResult
from stockfu.ai.operators.registry import register
from stockfu.services.factors import quote_series, percentile


@register
class LowVolatilityOperator(BaseOperator):
    operator_id = "low_volatility"
    type = "math"
    PARAMS_SCHEMA = {"window": 20, "hist_years": 3}   # 波动窗口 / 历史分位回溯年

    def run(self, ctx, params):
        window = int(params.get("window", 20))
        hist_years = int(params.get("hist_years", 3))
        closes = quote_series(ctx.code, "close", hist_years * 365 + window + 30,
                              as_of=ctx.as_of)
        if len(closes) < window + 1:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning=f"低波样本不足({len(closes)}<{window + 1})")
        rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
        if len(rets) < window:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning="收益率样本不足")

        # 滚动 window 日 std 序列(历史);末位 = 当前 std。
        # 优化(2026-08):原实现每窗口全遍历 O(N×window),window 越大越慢(lv_w30 比
        # lv_w10 慢 3 倍);改为增量滑窗维护 sum/sumsq,每窗口 O(1),整段 O(N)。
        # v2 修复:增量累积的浮点误差会让「停牌全 0 收益窗口」的 std 残留 ~1e-10
        # (而非精确 0),绕过 cur_std<=0 守卫把停牌股误判为极低波强买入 ——
        # gdv dy3 重跑收益 +187.9%→+146.9% 的元凶(000061 2007-06 停牌实证)。
        # 维护非零收益计数 nz:窗口全 0 → std 精确 0(与旧实现逐位一致);
        # 非全 0 窗口用滑窗值(误差 ~1e-17,分位 round 后无差)。
        n = window
        _s = 0.0
        _ss = 0.0
        nz = 0
        for _x in rets[:n]:
            _s += _x
            _ss += _x * _x
            if _x != 0.0:
                nz += 1
        std_series = []
        for _i in range(len(rets) - n + 1):
            if _i > 0:
                _out, _inn = rets[_i - 1], rets[_i + n - 1]
                _s += _inn - _out
                _ss += _inn * _inn - _out * _out
                if _out != 0.0:
                    nz -= 1
                if _inn != 0.0:
                    nz += 1
            if nz == 0:
                std_series.append(0.0)
            else:
                _var = _ss / n - (_s / n) ** 2
                std_series.append(max(_var, 0.0) ** 0.5)
        cur_std = std_series[-1] if std_series else 0.0
        if cur_std <= 0:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning="std 为 0(价格恒定/停牌)")
        pct, n = percentile(std_series, cur_std)
        if pct is None:
            return OpResult(operator=self.operator_id, type="math", value=None,
                            signal="hold", score=0.0, confidence=0.3,
                            reasoning=f"历史 std 样本不足({n})")
        # 低波动 → 正分(pct 越低 = 波动越小 = 越看多);与 value 低估同模板
        if pct < 30:
            score = 20 * (1 - pct / 30)
            signal = "buy"
            reasoning = f"波动率分位 {pct:.0f}% 偏低(低波动异象,风险调整后占优)"
        elif pct > 70:
            score = -20 * (1 - (100 - pct) / 30)
            signal = "sell"
            reasoning = f"波动率分位 {pct:.0f}% 偏高(高波动风险)"
        else:
            score = 0.0
            signal = "hold"
            reasoning = f"波动率分位 {pct:.0f}% 中性"
        return OpResult(operator=self.operator_id, type="math", value=round(pct, 1),
                        signal=signal, score=round(score, 1), confidence=0.6,
                        reasoning=reasoning)
