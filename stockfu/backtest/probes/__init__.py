"""回测探测脚本(Phase 1):故意游离于四层算子→策略→选股→执行正规管线之外的探索性回测。

放独立子包以示区别:这些脚本不进 scheduler.run / engine.run_backtest,只为快速验证某个
策略假设有无 edge。验证通过后再正式化进架构(算子 + rebalancer)。
"""
