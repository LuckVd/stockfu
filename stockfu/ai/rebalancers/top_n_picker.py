"""TopN 选股方案层:每日从全市场打分排序,选 Top N 持仓,带建仓锁定+限换手。

规则:
  1. 选股:    每日全市场按 raw 横截面百分位 × confidence 排序,目标持仓 = ranked 前 top_n
  2. 建仓锁定: 新买入 lock_days 个交易日内不清理(降换手),除非 risk_veto
  3. 限换手:   每日最多替换 max_replace 只(先清过 lock 的非 target 持仓腾位,再建 target 新票)
  4. 硬约束:   正值仓位数 ≤ top_n(末尾兜底,保 lock 期内的不被强清)

单一建仓路径(治持仓膨胀):只有 ③ 换入(必须先清 replaceable 腾位,受 max_replace 限)
和 ④ 空仓首日批量两条路径开新仓。历史 bug:多个建仓路径互不约束 + top10% 保护分母随
all_codes 漂移 → lock 期内只建不清 → 持仓膨胀到 top_n + lock×max_replace。

状态(回测全程同一实例,跨天保持):
  _day:        交易日计数器
  _entry_day:  {code: 建仓时的 _day}
"""
from __future__ import annotations

from stockfu.ai.rebalancers.base import Rebalancer, _cross_section_percentiles
from stockfu.ai.rebalancers.registry import register


@register
class TopNPicker(Rebalancer):
    """TopN 选股 + 建仓锁定 + 限换手。"""

    rebalancer_id = "top_n_picker"

    def __init__(self):
        self._day: int = 0
        self._entry_day: dict[str, int] = {}

    def adjust(self, desired, current, meta, equity, params):
        """desired ∪ current 全集 → 选 top_n 持仓 + lock 保护 + 限换手后的 final。"""
        self._day += 1
        top_n = int(params.get("top_n", 10))
        lock_days = int(params.get("lock_days", 20))
        max_replace = int(params.get("max_replace", 2))
        max_w = float(params.get("max_w", 0.20))

        all_codes = set(desired) | set(current)
        if not all_codes:
            return {}

        # ① 全市场排序:横截面百分位(raw)× confidence 降序
        pct = _cross_section_percentiles(meta, all_codes)
        ranked = self._rank_stocks(all_codes, meta, pct)
        target_set = set(ranked[:top_n])
        rank_pos = {c: i for i, c in enumerate(ranked)}

        held_codes = {c for c in all_codes if current.get(c, 0) > 0.001}
        final: dict[str, float | None] = {}
        replaceable: list[str] = []

        # ② 当前持仓分类:target 内保留满仓;非 target 的 lock 期内保留、过 lock 进 replaceable
        for code in held_codes:
            m = (meta or {}).get(code, {})
            if m.get("risk_vetoed"):
                final[code] = 0.0
                self._entry_day.pop(code, None)
                continue
            if code in target_set:
                d = desired.get(code)
                final[code] = max_w if (d is None or d > 0) else d   # target 内 → 目标满仓
                continue
            entry = self._entry_day.get(code)
            if entry is not None and (self._day - entry) < lock_days:
                final[code] = current[code]   # lock 期内,维持(降换手)
            else:
                replaceable.append(code)      # 过 lock 且非 target,可清理

        # ③ 换仓:清 replaceable(排名低优先)+ 建 target 未持仓(排名高优先),每日 ≤ max_replace
        replaceable.sort(key=lambda c: rank_pos.get(c, len(ranked)))
        candidates = [c for c in ranked if c in target_set and c not in held_codes]
        n = min(max_replace, len(replaceable), len(candidates))
        for i in range(n):
            final[replaceable[i]] = 0.0
            self._entry_day.pop(replaceable[i], None)
            new = candidates[i]
            final[new] = max_w
            self._entry_day[new] = self._day
        for code in replaceable[n:]:          # 没被清的 replaceable:维持
            final[code] = current[code]

        # ④ 空仓首日:批量建 top_n
        if not held_codes:
            for code in ranked[:top_n]:
                if code not in final:
                    final[code] = max_w
                    self._entry_day[code] = self._day

        # ⑤ 未覆盖的票:已持仓维持,其余不开仓(建仓只走 ③/④)
        for code in all_codes:
            if code not in final:
                final[code] = current[code] if code in held_codes else None

        # ⑥ 硬约束兜底:正值仓位数 ≤ top_n(lock 期内的保护,不强清)
        pos = [c for c, w in final.items() if w is not None and w > 0]
        if len(pos) > top_n:
            keep = set(ranked[:top_n])
            for c in pos:
                if c in keep:
                    continue
                entry = self._entry_day.get(c)
                if entry is not None and (self._day - entry) < lock_days:
                    continue   # lock 期内保护
                final[c] = 0.0
                self._entry_day.pop(c, None)

        return final

    @staticmethod
    def _rank_stocks(codes: set[str], meta: dict,
                     pct: dict[str, float]) -> list[str]:
        """按 横截面百分位 × confidence 降序排列, risk_vetoed 沉底。

        百分位用未 clamp 的 raw 算(头部连续可分,治撞顶同分);code 作最终 tiebreaker 保可复现。
        """
        def _priority(code: str) -> float:
            m = (meta or {}).get(code, {})
            if m.get("risk_vetoed"):
                return -99999.0
            return pct.get(code, 0.0) * (m.get("confidence") or 0.0)

        return sorted(codes, key=lambda c: (-_priority(c), c))
