"""仓位调整层(rebalancers/):可插拔的「仓位调整方案」库。

和算子层(operators/)、策略层(strategies/)平级的基础架构层。
方案用 Python @register 注册(算法逻辑,YAML 写不了);配置走 app_config,解耦于策略。
active 由 app_config('active_rebalancer_id') 指针决定(复刻 active_strategy_id)。
"""
from stockfu.ai.rebalancers.base import Rebalancer
from stockfu.ai.rebalancers.registry import (
    REGISTRY,
    register,
    discover_and_register,
    get_rebalancer,
    get_active_rebalancer,
    get_rebalancer_params,
)

__all__ = [
    "Rebalancer",
    "REGISTRY",
    "register",
    "discover_and_register",
    "get_rebalancer",
    "get_active_rebalancer",
    "get_rebalancer_params",
]
