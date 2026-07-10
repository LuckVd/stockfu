"""风险 LLM 算子(包装 RiskAdvisor)。其 sell/strong_sell 触发一票否决(veto=True)。"""
from stockfu.ai.operators.llm.base import LLMOperator
from stockfu.ai.operators.registry import register
from stockfu.ai.operators.llm.advisors import RiskAdvisor


@register
class RiskOperator(LLMOperator):
    operator_id = "risk"
    type = "llm"
    advisor_cls = RiskAdvisor
