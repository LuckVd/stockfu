"""downside_volatility:近 N 日「负收益」半标准差 × √252 年化(%,qfq)。

下行偏差(downside deviation / semivariance)只度量不利一侧的波动,是 Sortino
分母与防御型投资者真正关心的风险维度。与 low_volatility(总波动)不同——总波动
相同但下行更小的股票更优。direction=lower_is_better,本层只产 raw=年化下行波动%。
"""
from __future__ import annotations

import math
from datetime import date

from stockfu.factors.raw import raw_fingerprint
from stockfu.scoring.contracts import MissingReason, RawFactorObservation

METRIC_ID = "downside_volatility"
_WINDOW = 60


def compute_downside_volatility(code: str, as_of: date, window: int = _WINDOW,
                                price_basis: str = "qfq") -> RawFactorObservation:
    window = int(window)
    if window <= 1 or price_basis != "qfq":
        raise ValueError("downside_volatility 的 window/price_basis 参数无效")
    fp = raw_fingerprint(METRIC_ID, "neg_ret_std_x_sqrt252_x100",
                         {"window": window, "price_basis": price_basis})
    from stockfu.services.factors import quote_series

    span = int(window * 1.5) + 30
    closes = quote_series(code, "close", span, as_of=as_of)
    if len(closes) < window + 1:
        return RawFactorObservation(
            asset_code=code, as_of=as_of, raw_metric_id=METRIC_ID,
            raw_value=None, raw_unit="annualized_downside_vol_percent",
            source_max_date=as_of, available_at=as_of, valid=False,
            missing_reason=MissingReason.INSUFFICIENT_SAMPLES, raw_fingerprint=fp,
            diagnostics={"n_closes": len(closes)})
    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))
            if closes[i - 1] > 0]
    recent = rets[-window:]
    neg = [r for r in recent if r < 0]
    # 下行方差:以全部 window 个观测为分母(标准半方差定义),只对负收益求平方和
    var = sum(r * r for r in neg) / window
    std = math.sqrt(max(var, 0.0))
    annual = std * math.sqrt(252.0) * 100.0
    if std <= 0.0:
        # 窗口内无负收益 → 下行风险为 0(有效极低值,非缺失)
        return RawFactorObservation(
            asset_code=code, as_of=as_of, raw_metric_id=METRIC_ID,
            raw_value=0.0, raw_unit="annualized_downside_vol_percent",
            source_max_date=as_of, available_at=as_of, valid=True, raw_fingerprint=fp,
            lookback_observations=window,
            diagnostics={"n_neg": len(neg), "window": window})
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=METRIC_ID,
        raw_value=float(annual), raw_unit="annualized_downside_vol_percent",
        source_max_date=as_of, available_at=as_of, valid=True, raw_fingerprint=fp,
        lookback_observations=window,
        diagnostics={"n_neg": len(neg), "daily_downside_std_pct": round(std * 100.0, 4)})
