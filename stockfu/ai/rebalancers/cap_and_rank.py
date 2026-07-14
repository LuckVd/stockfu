"""总仓位上限 + 超配按优先级分配。

复用自旧 portfolio.py 的 CapAndRank 算法(只改方法名 allocate→adjust、id 名):
  - 减仓/清仓(desired <= current,含 risk_vetoed 清仓 desired=0):永远直接放行,
    释放资金、风险优先,不参与额度竞争。
  - 增仓/建仓(desired > current):按 (横截面百分位×confidence, code) 降序竞争 max_gross 额度;
    额度不够的尾部砍掉 → 回退 current(维持,不写 0,不误清)。
    code 作 tiebreaker 保证回测可复现。
  - 未覆盖持仓(current 有 desired 无):维持(None),不主动清。

max_gross:持仓最多占总资产的比例(默认 0.95);现金底线 = 1 - max_gross。
"""
from __future__ import annotations

from stockfu.ai.rebalancers.base import Rebalancer, _cross_section_percentiles
from stockfu.ai.rebalancers.registry import register


@register
class CapAndRank(Rebalancer):
    rebalancer_id = "cap_and_rank"

    def adjust(self, desired, current, meta, equity, params):
        max_gross = float((params or {}).get("max_gross", 0.95))
        final: dict[str, float | None] = {}
        buy_requests: list[tuple[float, str, float]] = []  # (priority, code, desired)

        # 横截面百分位(raw 连续强度)→ 增仓竞争优先级(头部连续可分,治撞顶同分)
        all_codes = set(desired) | set(current)
        pct = _cross_section_percentiles(meta, all_codes)
        # 第一遍:遍历全集。减仓/清仓/维持 放行;增仓 进竞争池;未覆盖→维持
        for code in all_codes:
            cur = current.get(code, 0.0)
            d = desired.get(code)               # None = 未覆盖 / 策略说维持
            if d is None:
                final[code] = None              # 维持(含未覆盖)
            elif d <= cur:
                final[code] = d                 # 减仓/清仓放行(不占额度)
            else:
                final[code] = cur               # 占位=维持,中选后覆盖
                m = (meta or {}).get(code, {})
                conf = m.get("confidence")
                conf = conf if conf is not None else 0.0
                buy_requests.append((pct.get(code, 0.0) * conf, code, d))

        # 放行后的总仓位:维持用 current,减仓用 d,增仓占位用 current
        running_gross = sum(
            current.get(c, 0.0) if final[c] is None else final[c]
            for c in final
        )

        # 按 priority 降序 + code 升序 竞争额度
        for _priority, code, d in sorted(buy_requests, key=lambda x: (-x[0], x[1])):
            cur = current.get(code, 0.0)
            increment = d - cur
            if running_gross + increment <= max_gross:
                final[code] = d                 # 全额满足
                running_gross += increment
            else:
                final[code] = cur               # 砍:维持现状,不误清

        return final
