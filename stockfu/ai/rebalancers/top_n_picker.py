"""TopN 选股方案层：每日从全市场打分排序，选 Top N 持仓，带建仓锁定+限换手。

规则(用户设定):
  1. 建仓锁定: 新买入 20 个交易日内不能卖出(除非 risk_veto 风控)，只能持有或加仓
  2. Top 保护: 持仓中排名全市场前 10% 的不可被替换
  3. 限换手:   每日最多替换 2 只

状态维护(实例变量,回测全程同一实例):
  _day:        交易日计数器(每次 adjust 调用 +1)
  _entry_day: {code: 建仓时的 _day 值}

使用:
  set_app_config('active_rebalancer_id', 'top_n_picker')
  set_app_config('rebalancer_params', '{"top_n": 10, "lock_days": 20, "max_replace": 2, "max_w": 0.20}')

和 pass_through / cap_and_rank 同级,对其他层零改动。
"""
from __future__ import annotations

from stockfu.ai.rebalancers.base import Rebalancer
from stockfu.ai.rebalancers.registry import register


@register
class TopNPicker(Rebalancer):
    """TopN 选股+建仓锁定+限换手。"""

    rebalancer_id = "top_n_picker"

    def __init__(self):
        # 状态——回测全程同一个实例,跨天保持
        self._day: int = 0
        self._entry_day: dict[str, int] = {}  # code → 建仓日(_day 值)

    # ================================================================
    # 公开接口
    # ================================================================

    def adjust(self, desired, current, meta, equity, params):
        """desired ∪ current 全集 → 选股调仓后的 final。"""
        self._day += 1
        top_n = int(params.get("top_n", 10))
        lock_days = int(params.get("lock_days", 20))
        max_replace = int(params.get("max_replace", 2))
        max_w = float(params.get("max_w", 0.20))

        all_codes = set(desired) | set(current)
        if not all_codes:
            return {}

        # ── ① 全市场排序(score×confidence 降序, risk_vetoed 沉底) ──
        ranked = self._rank_stocks(all_codes, meta)
        target_set = set(ranked[:top_n])                 # 想持有的
        top10pct = set(ranked[:max(1, len(ranked) // 10)])  # 前 10% 保护线

        # ── ② 分析当前持仓 ──
        held_codes = {c for c in all_codes if current.get(c, 0) > 0.001}

        # 结果容器
        final: dict[str, float | None] = {}

        # 分类: 必须保留的, 可替换的
        must_keep: set[str] = set()
        replaceable: list[str] = []  # 按排名从低到高(先换掉最差的)

        for code in held_codes:
            cur_w = current[code]
            d = desired.get(code)     # None = 策略没覆盖

            # 风控优先
            m = (meta or {}).get(code, {})
            if m.get("risk_vetoed"):
                final[code] = 0.0
                # 清仓了, 解锁
                self._entry_day.pop(code, None)
                continue

            # 建仓锁定检查
            entry = self._entry_day.get(code)
            is_locked = entry is not None and (self._day - entry) < lock_days

            if is_locked:
                # 锁定中: 只准加仓或维持, 不准减仓
                if d is not None and d > cur_w:
                    final[code] = min(d, max_w)
                else:
                    final[code] = cur_w
                must_keep.add(code)
                continue

            # Top 10% 保护检查
            if code in top10pct:
                # 允许按 desired 调整(含减仓), 但不主动替换
                if d is not None:
                    final[code] = min(d, max_w) if d > 0 else d
                else:
                    final[code] = cur_w
                must_keep.add(code)
                continue

            # 可替换: 是否在目标集合里
            if code in target_set:
                # 虽然可替换但还在目标内, 保留
                if d is not None:
                    final[code] = min(d, max_w) if d > 0 else d
                else:
                    final[code] = cur_w
            else:
                # 标记为可替换, 按排名由低到高排序
                replaceable.append(code)

        # 可替换的按排名排序(低分优先被换)
        rank_pos = {c: i for i, c in enumerate(ranked)}
        replaceable.sort(key=lambda c: rank_pos.get(c, 9999))

        # ── ③ 执行替换 ──
        # 候选新标的: 目标集合中尚未持仓且未进入 final 的
        candidates = [
            c for c in ranked
            if c in target_set
            and c not in held_codes
            and c not in final
            and current.get(c, 0) <= 0.001
        ]

        n_replace = min(max_replace, len(replaceable), len(candidates))

        # 被换掉的 -> 清仓
        for i in range(n_replace):
            old = replaceable[i]
            final[old] = 0.0

        # 换入新的 -> 建仓
        for i in range(n_replace):
            new = candidates[i]
            final[new] = max_w
            if current.get(new, 0) <= 0.001:
                self._entry_day[new] = self._day

        # 剩余可替换但未替换的: 维持
        for code in replaceable[n_replace:]:
            final[code] = current.get(code, 0.0)

        # ── ④ 首次运行: 无持仓时批量建仓 ──
        if not held_codes:
            for code in ranked[:top_n]:
                if code not in final:
                    final[code] = max_w
                    self._entry_day[code] = self._day

        # ── ⑤ 补充未覆盖的 desired 股票(策略有信号但没持仓的) ──
        # 注意: 只处理已进入 final 的, 不额外开新仓(防止超 max_replace)
        for code in all_codes:
            if code not in final:
                d = desired.get(code)
                if d is not None and d > 0 and code in target_set:
                    # 策略看好且在目标集合内, 建仓
                    final[code] = min(d, max_w)
                    if current.get(code, 0) <= 0.001:
                        self._entry_day[code] = self._day
                elif code in held_codes:
                    final[code] = current[code]  # 维持
                else:
                    final[code] = d if d is not None else 0.0

        return final

    # ================================================================
    # 内部
    # ================================================================

    @staticmethod
    def _rank_stocks(codes: set[str],
                     meta: dict) -> list[str]:
        """按 score×confidence 降序排列, risk_vetoed 沉底。"""
        def _priority(code: str) -> float:
            m = (meta or {}).get(code, {})
            if m.get("risk_vetoed"):
                return -99999.0
            s = m.get("score") or 0.0
            c = m.get("confidence") or 0.0
            return s * c

        return sorted(codes, key=_priority, reverse=True)
