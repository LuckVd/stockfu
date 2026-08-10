"""rsi:Wilder RSI(N)(0-100,qfq 收盘)。

RSI 衡量一段时期内上涨幅度的相对强弱(Wilder 1978),N=14 为业界默认。
均值回归用法:RSI 低(超卖)→ 后续反弹概率高,故 direction=lower_is_better
(由 profile 决定);本层只产 raw=RSI 值。Wilder 平滑需要预热,取 5×N 序列稳定平滑。
"""
from __future__ import annotations

from datetime import date

from stockfu.factors.raw import raw_fingerprint
from stockfu.scoring.contracts import MissingReason, RawFactorObservation

METRIC_ID = "rsi"


def _wilder_rsi(closes: list[float], n: int) -> float | None:
    """增量 Wilder RSI:返回序列末位(当日 RSI)。样本<n+1 → None。"""
    if len(closes) < n + 1:
        return None
    diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    avg_g = sum(max(d, 0.0) for d in diffs[:n]) / n
    avg_l = sum(max(-d, 0.0) for d in diffs[:n]) / n
    for d in diffs[n:]:
        avg_g = (avg_g * (n - 1) + max(d, 0.0)) / n
        avg_l = (avg_l * (n - 1) + max(-d, 0.0)) / n
    if avg_l == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_g / avg_l)


def compute_rsi(code: str, as_of: date, window: int = 14,
                price_basis: str = "qfq") -> RawFactorObservation:
    window = int(window)
    if window <= 0 or price_basis != "qfq":
        raise ValueError("rsi 的 window 参数无效或 price_basis 非 qfq")
    fp = raw_fingerprint(METRIC_ID, "wilder_rsi",
                         {"window": window, "price_basis": price_basis})
    from stockfu.services.factors import quote_series

    span = int(window * 5 * 1.5) + 30           # 5×N 预热让 Wilder 平滑稳定
    closes = quote_series(code, "close", span, as_of=as_of)
    if len(closes) < window + 1:
        return RawFactorObservation(
            asset_code=code, as_of=as_of, raw_metric_id=METRIC_ID,
            raw_value=None, raw_unit="rsi", source_max_date=as_of,
            available_at=as_of, valid=False,
            missing_reason=MissingReason.INSUFFICIENT_SAMPLES, raw_fingerprint=fp,
            diagnostics={"n_closes": len(closes)})
    cur = _wilder_rsi(closes, window)
    if cur is None:
        return RawFactorObservation(
            asset_code=code, as_of=as_of, raw_metric_id=METRIC_ID,
            raw_value=None, raw_unit="rsi", source_max_date=as_of,
            available_at=as_of, valid=False,
            missing_reason=MissingReason.INSUFFICIENT_SAMPLES, raw_fingerprint=fp,
            diagnostics={"n_closes": len(closes)})
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=METRIC_ID,
        raw_value=float(cur), raw_unit="rsi",
        source_max_date=as_of, available_at=as_of, valid=True, raw_fingerprint=fp,
        lookback_observations=window,
        diagnostics={"window": window, "rsi": round(cur, 3)})
