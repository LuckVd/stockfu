"""LLM 顾问算子(首阶段)。

run_with_tools 从 ai/analyze.py 迁入(加 prompt_override 支持 DB 热改 prompt);
LLMOperator 基类组合现有 BaseAdvisor(复用 build_user_message/parse),
run() → 工具循环 → Opinion → OpResult。4 个顾问薄包装各绑一个 advisor_cls。

缓存:LLM 算子结果按 (code,as_of) 落 ai_report 表(由 scheduler._make_cached_analyze
统一处理),与现有实盘/回测缓存机制完全一致——本模块不感知缓存。
"""
from stockfu.ai.operators.llm.base import LLMOperator, run_with_tools

__all__ = ["LLMOperator", "run_with_tools"]
