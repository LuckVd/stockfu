"""算子化量化平台: 算子库 + 注册表 + 策略编排。

两类算子(math 数学因子 / aggregator 汇总)共用 BaseOperator 协议,靠 type 字段区分。
算子实现留本包代码,DB(operator 表)只存可热改元数据(参数 schema / display)。
策略用 YAML 编排,runner.compile_strategy 编译成 analyze_fn 注入回测引擎。

入口:
  discover_and_register()   # 扫描 operators/{factors,aggregators}/ 注册算子
  REGISTRY                  # {operator_id: OperatorClass}
  compile_strategy(yaml)    # YAML → CompiledStrategy(runner.py)
"""
from stockfu.ai.operators.base import BaseOperator, OpContext, OpResult
from stockfu.ai.operators.registry import (
    REGISTRY, discover_and_register, get_operator_class, register,
)

__all__ = [
    "BaseOperator", "OpContext", "OpResult",
    "REGISTRY", "register", "discover_and_register",
    "get_operator_class",
]
