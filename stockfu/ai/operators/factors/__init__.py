"""数学因子算子(首阶段): 纯函数,score 尺度对齐 LLM 算子(-20~+20)以便混合作证。

新增 4 个传统因子(momentum/mean_reversion/value/trend_strength),供 pure_factor /
hybrid 策略使用。现有 7 工具/composite 因子的"包装成数学算子"留后续阶段。
模块导入即触发 @register。
"""
