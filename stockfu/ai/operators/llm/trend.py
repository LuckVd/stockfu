"""趋势 LLM 算子(包装 TrendAdvisor)。prompt 由 runner 从 operator 表注入。"""
from stockfu.ai.operators.llm.base import LLMOperator
from stockfu.ai.operators.registry import register
from stockfu.ai.operators.llm.advisors import TrendAdvisor


@register
class TrendOperator(LLMOperator):
    operator_id = "trend"
    type = "llm"
    advisor_cls = TrendAdvisor
