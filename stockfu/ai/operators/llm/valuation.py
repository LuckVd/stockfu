"""估值 LLM 算子(包装 ValuationAdvisor)。"""
from stockfu.ai.operators.llm.base import LLMOperator
from stockfu.ai.operators.registry import register
from stockfu.ai.operators.llm.advisors import ValuationAdvisor


@register
class ValuationOperator(LLMOperator):
    operator_id = "valuation"
    type = "llm"
    advisor_cls = ValuationAdvisor
