"""汇总算子基类与共享辅助。

aggregator 不消费 OpContext 取数,而是汇总其他算子的 OpResult,故实现
aggregate(results, params);run() 不用(runner 识别 type=aggregator 走 aggregate)。
"""
from __future__ import annotations

from stockfu.ai.operators.base import BaseOperator, OpResult

# 默认 total_score → signal 阈值(复用 synthesis._THRESHOLDS 口径)
_DEFAULT_THRESHOLDS = {"strong_buy": 15, "buy": 5, "hold": -5, "sell": -15}


class Aggregator(BaseOperator):
    """汇总算子基类。"""
    type = "aggregator"

    def run(self, ctx, params):  # noqa: D401
        raise NotImplementedError("aggregator 用 aggregate(results, params),不走 run")

    def aggregate(self, results: list[OpResult], params: dict) -> OpResult:
        raise NotImplementedError


def score_to_signal(total: float, thresholds: dict | None) -> str:
    """total_score → final_signal(从高到低命中,复现 synthesis._THRESHOLDS 逻辑)。"""
    th = {**_DEFAULT_THRESHOLDS, **(thresholds or {})}
    if total >= th["strong_buy"]:
        return "strong_buy"
    if total >= th["buy"]:
        return "buy"
    if total >= th["hold"]:
        return "hold"
    if total >= th["sell"]:
        return "sell"
    return "strong_sell"


def collect_meta(results: list[OpResult]) -> tuple[float | None, float | None]:
    """汇总综合 confidence(均值) + ai_target_weight(取 score>0 中 confidence 最高的)。

    signal 已降级为派生标签(不参与决策),看多筛选改用 score 符号。
    continuous 模式下 ai_tw 不参与仓位决策(走 total_score 连续映射),此处仅保留兼容。
    """
    confs = [r.confidence for r in results if r.confidence is not None]
    confidence = sum(confs) / len(confs) if confs else None
    bullish = [r for r in results if r.score > 0 and r.target_weight is not None]
    ai_tw = max(bullish, key=lambda r: r.confidence).target_weight if bullish else None
    return confidence, ai_tw
