"""stockfu AI 4 顾问:趋势 / 逆向 / 风险 / 估值(operators/llm 镜像,逐字复制自 skills/advisors/)。

每个顾问是一个常驻角色 skill:拿到 AdvisorContext → 拼 system_prompt → 调 LLM → 解析成 Opinion。
4 个 Opinion 由后续的 synthesis(综合决策)合成最终建议。
"""
from stockfu.ai.operators.llm.advisors.base import AdvisorContext, BaseAdvisor, Opinion
from stockfu.ai.operators.llm.advisors.contrarian import ContrarianAdvisor
from stockfu.ai.operators.llm.advisors.risk import RiskAdvisor
from stockfu.ai.operators.llm.advisors.trend import TrendAdvisor
from stockfu.ai.operators.llm.advisors.valuation import ValuationAdvisor

# 常驻顾问清单(每次分析都跑这 4 个,不走路由 —— 这是与 daily 15 策略的根本区别)
ALL_ADVISORS = [TrendAdvisor, ContrarianAdvisor, RiskAdvisor, ValuationAdvisor]

__all__ = [
    "AdvisorContext",
    "Opinion",
    "BaseAdvisor",
    "TrendAdvisor",
    "ContrarianAdvisor",
    "RiskAdvisor",
    "ValuationAdvisor",
    "ALL_ADVISORS",
]
