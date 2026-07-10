"""汇总算子(首阶段): list[OpResult] → final_signal/total_score/veto。

复现 synthesis.aggregate 的规则汇总逻辑(加权 + risk 一票否决),使其结果与
现有 engine 期望的 aggregate dict 契约一致。模块导入即触发 @register。
"""
