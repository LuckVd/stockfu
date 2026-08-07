"""V2 因子评分与回测架构(stockfu/scoring/)。

设计依据:docs/SPECS/factor-strategy-score-v2.md。本包只负责
0–100 量纲契约:原始值 → 因子分 → (策略分在 stockfu/strategy/)。
不依赖 V1 OpResult.score 语义。
"""
