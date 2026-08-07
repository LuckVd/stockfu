"""V2 纯原始因子计算器(stockfu/factors/raw/)。

每个计算器返回 RawFactorObservation(纯原始值 + 数据截止日),
不输出 score、不知道 0–100、不知道策略权重(设计 §4 职责边界)。
"""
