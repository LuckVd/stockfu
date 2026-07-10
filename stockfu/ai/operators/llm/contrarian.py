"""逆向 LLM 算子(包装 ContrarianAdvisor)。"""
from stockfu.ai.operators.llm.base import LLMOperator
from stockfu.ai.operators.registry import register
from stockfu.ai.operators.llm.advisors import ContrarianAdvisor


@register
class ContrarianOperator(LLMOperator):
    operator_id = "contrarian"
    type = "llm"
    advisor_cls = ContrarianAdvisor
