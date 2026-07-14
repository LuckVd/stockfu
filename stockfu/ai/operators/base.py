"""算子基类与数据契约。

三类算子(math/llm/aggregator)共用 BaseOperator 骨架,靠 type 字段区分行为。
OpResult 同时兼容现有 Opinion(score_adjustment/signal/confidence/target_weight)
与 synthesis.aggregate 输出(final_signal/total_score/risk_vetoed),使回测引擎
(engine.py Phase 3 读 aggregate)零改动即可消费算子 pipeline 的产出——这是
"重构替换但不破回测"的关键契约。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass
class OpContext:
    """喂给算子的统一数据包(只读)。算子不直接取库,数据由 StrategyRunner 填充。

    as_of: 防未来函数上界(None=实盘/今天);透传给 quote_series/build_context/工具。
    factors: 数学算子预计算结果的共享视图(key=算子id),LLM 算子可读(混合作证增强用)。
    advisor_ctx: 兼容老 AdvisorContext(LLM 算子复用 build_context 产出,零改动)。
    """
    code: str
    name: str = ""
    as_of: date | None = None
    factors: dict[str, Any] = field(default_factory=dict)
    series: dict[str, list[float]] = field(default_factory=dict)  # 预填序列(阶段2 run_batch 用,首阶段空)
    advisor_ctx: Any = None                # AdvisorContext(LLM 算子用)


@dataclass
class OpResult:
    """所有算子的统一输出。字段兼容 Opinion + aggregate,使 engine 零改动消费。

    score: 该算子对总分的加权前贡献(LLM 算子=-20~+20 的 score_adjustment;
           数学算子由强度×基准算出,Aggregator 汇总时再乘 YAML 配的 weight)。
    raw_score: 未 clamp 的连续强度(排序用)。score 被 ±20 clamp 会压平头部区分度,
           raw_score 保留 clamp 前的连续值供 rebalancer 横截面排名;None=该算子无
           连续信息(离散算子/LLM/旧缓存)→ 聚合时退化为 score。
    value: 数学算子的原始数值(RSI=32/momentum_pct=65),供前端展示/批量预计算/调试。
    veto:  一票否决位(risk 类算子或 risk_veto aggregator 设置 → Aggregator 强制 sell)。
    """
    operator: str
    type: str = "math"                     # math | llm | aggregator
    signal: str = "hold"                   # strong_buy/buy/hold/sell/strong_sell
    score: float = 0.0
    raw_score: float | None = None         # 未 clamp 连续强度(排序用);None→退化 score
    weight: float = 1.0                    # 策略 YAML 配的权重(汇总时用)
    confidence: float = 0.5                # 0-1
    reasoning: str = ""
    evidence: dict = field(default_factory=dict)
    tools_used: list = field(default_factory=list)   # LLM 算子工具调用记录(math 空)
    target_weight: float | None = None
    value: float | None = None             # 数学算子原始值
    veto: bool = False


class BaseOperator:
    """算子基类。子类设置 operator_id/type 并实现 run()。

    PARAMS_SCHEMA: 参数 JSON Schema(同步入库 operator.params_schema,供 YAML 校验 +
                   默认值填充)。数学算子在此声明 window/period 等;LLM 算子一般空。
    run_batch: 批量预计算接口(后续"快速回测"阶段实现向量化;首阶段不实现,默认 raise)。
    """
    operator_id: str = ""
    type: str = "math"                     # math | llm | aggregator
    PARAMS_SCHEMA: dict = {}

    def run(self, ctx: OpContext, params: dict) -> OpResult:  # noqa: D401
        raise NotImplementedError(f"{type(self).__name__}.run 未实现")

    def run_batch(self, codes: list[str], as_of_list: list[date],
                  params: dict) -> dict:
        """批量预计算(快速回测阶段用向量化实现)。

        首阶段不实现——回测仍按 (code,as_of) 逐个调 run(经 ai_report/factor_snapshot 缓存)。
        """
        raise NotImplementedError(f"{type(self).__name__}.run_batch 未实现(后续阶段)")
