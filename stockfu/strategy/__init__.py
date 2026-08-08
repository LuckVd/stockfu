"""V2 策略层(stockfu/strategy/)。

三类独立身份(设计 §13.1):alpha(评分)/ portfolio_policy(仓位)/ risk_policy(敞口)。
本层只消费 stockfu/scoring 输出的 0–100 因子分,不重算分数。
"""
