"""turnover_20d：近 N 个交易日换手率均值（%，原始口径）。

换手/注意力因子（2026-08-16 接入，判别文档 docs/SPECS/turnover-attention-ic.md）：
IC 快验显示 turn20 是独立因子维度——对 5 日视界强负预测（拥挤惩罚）、
对 20 日视界强正预测（关注度-流动性溢价），非动量/波动率代理。
本层只产 raw=平均换手率 %，方向由 profile（higher_is_better）决定。

口径注意：
- turnover 为东财/baostock 原始换手率（%），无复权概念，直接取原始列；
- 停牌日无行情行（序列天然缺行），turnover=0 视为有效值（当日无成交）；
- 缺失容忍：window 内有效值 >=MIN_VALID_N 才出值（2013-2017 段 turnover
  覆盖 87-96%，严格要满 window 会系统性缺失）；不足则 INSUFFICIENT_SAMPLES。

同参数族复用：与 momentum 相同，同一函数可按 metric_id 注册为多个 raw metric
（V2 配置校验要求同一 metric_id 只能绑定一组参数）。
"""
from __future__ import annotations

from datetime import date

from stockfu.factors.raw import raw_fingerprint
from stockfu.scoring.contracts import MissingReason, RawFactorObservation

METRIC_ID = "turnover_20d"
_WINDOW = 20
MIN_VALID_N = 15


def compute_turnover_20d(code: str, as_of: date, window: int = _WINDOW,
                         min_valid: int = MIN_VALID_N,
                         metric_id: str = METRIC_ID) -> RawFactorObservation:
    window = int(window)
    min_valid = int(min_valid)
    if window <= 0 or min_valid <= 0 or min_valid > window:
        raise ValueError("turnover_20d 的 window/min_valid 参数无效")
    fp = raw_fingerprint(
        metric_id, "mean_turnover_pct",
        {"window": window, "min_valid": min_valid, "metric_id": metric_id},
    )
    from stockfu.services.factors import quote_series

    # 日历日缓冲按交易日/日历日≈252/365 放大 1.5 倍 + 余量（与 low_volatility 同口径）。
    span = int(window * 1.5) + 30
    turns = quote_series(code, "turnover", span, as_of=as_of)
    if len(turns) < min_valid:
        return RawFactorObservation(
            asset_code=code, as_of=as_of, raw_metric_id=metric_id,
            raw_value=None, raw_unit="turnover_percent", source_max_date=as_of,
            available_at=as_of, valid=False,
            missing_reason=MissingReason.INSUFFICIENT_SAMPLES, raw_fingerprint=fp,
            diagnostics={"n_turns": len(turns), "need": min_valid})
    recent = turns[-window:]
    if len(recent) < min_valid:
        return RawFactorObservation(
            asset_code=code, as_of=as_of, raw_metric_id=metric_id,
            raw_value=None, raw_unit="turnover_percent", source_max_date=as_of,
            available_at=as_of, valid=False,
            missing_reason=MissingReason.INSUFFICIENT_SAMPLES, raw_fingerprint=fp,
            diagnostics={"n_turns": len(recent), "need": min_valid})
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=metric_id,
        raw_value=float(sum(recent) / len(recent)),
        raw_unit="turnover_percent", source_max_date=as_of, available_at=as_of,
        valid=True, raw_fingerprint=fp,
        diagnostics={"window_used": len(recent)})
