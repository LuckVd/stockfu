"""算子基类与数据契约。

math/aggregator 算子共用 BaseOperator 骨架(回测侧 LLM 已下线)。OpResult 是回测
管线的统一输出;engine.py Phase3 读 aggregate dict(final_signal/total_score/
risk_vetoed/...)消费。实盘 AI 4 顾问用 Opinion,另走 ai/skills 链路,不经此契约。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass
class OpContext:
    """喂给算子的统一数据包(只读)。算子不直接取库,数据由 StrategyRunner 填充。

    as_of: 防未来函数上界(None=实盘/今天);透传给 quote_series/build_context/工具。
    factors: 数学算子预计算结果的共享视图(key=算子id)。
    """
    code: str
    name: str = ""
    as_of: date | None = None
    factors: dict[str, Any] = field(default_factory=dict)


@dataclass
class OpResult:
    """算子的统一输出(回测算子管线;实盘 AI 4 顾问用 Opinion,另走 ai/skills)。

    score: 连续强度(不 clamp;原 raw_score 已并入)。各算子保留各自满强度刻度
           (如 momentum ±20=±10%涨幅),Aggregator 加权汇总时乘 YAML weight。
    signal: 派生标签(从 score 阈值生成,仅供展示/审计),不参与仓位决策(连续映射用 score)。
    value: 数学算子的原始数值(RSI=32/momentum_pct=65),供 ctx.factors 共享 + 调试。
    veto:  一票否决位(risk 类算子 / risk_veto aggregator 设置)。
    """
    operator: str
    type: str = "math"                     # math | aggregator
    signal: str = "hold"                   # 派生标签(展示用,不参与决策)
    score: float = 0.0                     # 连续强度(不 clamp)
    weight: float = 1.0                    # 策略 YAML 权重(汇总用,不入库)
    confidence: float = 0.5                # 0-1
    reasoning: str = ""
    target_weight: float | None = None
    value: float | None = None             # 数学算子原始值(喂 ctx.factors)
    veto: bool = False


class BaseOperator:
    """算子基类。子类设置 operator_id/type 并实现 run()。

    PARAMS_SCHEMA: 参数 JSON Schema(同步入库 operator.params_schema,供 YAML 校验 +
                   默认值填充)。数学算子在此声明 window/period 等。
    """
    operator_id: str = ""
    type: str = "math"                     # math | aggregator
    PARAMS_SCHEMA: dict = {}

    def run(self, ctx: OpContext, params: dict) -> OpResult:  # noqa: D401
        raise NotImplementedError(f"{type(self).__name__}.run 未实现")
