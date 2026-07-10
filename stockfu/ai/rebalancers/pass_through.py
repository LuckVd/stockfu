"""透传方案:desired 原样透传,未覆盖持仓维持(None)。等价于"无组合层"。"""
from __future__ import annotations

from stockfu.ai.rebalancers.base import Rebalancer
from stockfu.ai.rebalancers.registry import register


@register
class PassThrough(Rebalancer):
    """透传 + 未覆盖维持(默认 active,保向后兼容)。

    final[code] = desired.get(code):desired 有就用 desired(含 None=策略说维持);
    desired 无(未覆盖持仓)→ None(维持)。
    与"完全没有仓位调整层"逐笔等价(should_act 收 None → no_target → 不动)。
    """
    rebalancer_id = "pass_through"

    def adjust(self, desired, current, meta, equity, params):
        final: dict[str, float | None] = {}
        for code in set(desired) | set(current):
            final[code] = desired.get(code)   # desired 无 → None(维持)
        return final
