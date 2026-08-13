"""换手抑制层(有状态 Rebalancer,组合执行控制)。

插在 portfolio/risk 产出的「理想目标」与 engine 下单的「实际订单」之间:给定 ideal
target + 当前持仓 + 持仓状态(建仓日/最近买入日)+ 浮亏,按组合政策的旋钮决定「今天
实际调哪些」:
  - rebalance_drift :偏离阈值(边沿触发),|目标-当前|≤此值不调 → 抑制机械再平衡
  - cooldown_days   :买入冷却,建仓/加仓后 N 日内不再加仓
  - min_holding_days:最小持仓(软锁),建仓后 N 个交易日内不卖/不清仓——但浮亏 ≥ stop_loss_pct
                     时豁免(该止损能止损,不扛回撤)
  - stop_loss_pct   :大跌豁免阈值,浮亏 ≥ 此值解除 min_holding 锁(配套软锁)
  - hold_top_percentile:持仓仍在当日票池前 N% 时,普通调仓不卖出
  - max_replace      :每个决策日最多进入/退出的股票数;0=关
  - max_single_weight:实际持仓超过此上限时强制降回上限
这些旋钮默认 0/None = 关闭,decide 退化为「全量目标 + 清仓」,等价旧 engine 行为。

只控换手(执行质量),不改 strategy_score、不改理想选股(§4 不变量)。
浮亏由 engine 用 Position.avg_cost + 当日收盘价算好传入(pnl_pct),rebalancer 不记成本。
状态(holding_since/holding_since_session/last_buy_date)由 engine 在成交后经
record_buy/record_close 回填；min_holding_days 使用交易日序号，不消耗周末和节假日。
"""
from __future__ import annotations

from datetime import date


class Rebalancer:
    def __init__(self, policy) -> None:
        self.policy = policy
        self.holding_since: dict[str, date] = {}    # code -> 建仓日(首次买入)
        self.holding_since_session: dict[str, int] = {}  # code -> 建仓交易日序号
        self.last_buy_date: dict[str, date] = {}    # code -> 最近一次买入日

    def _min_holding_locked(self, c: str, as_of: date, mhd: int,
                            pnl_pct: dict[str, float], sl,
                            trading_day_index: int | None = None) -> bool:
        """min_holding 是否锁住 c:锁定期内 且 未触发大跌豁免。"""
        if mhd <= 0:
            return False
        hs_session = self.holding_since_session.get(c)
        if trading_day_index is not None and hs_session is not None:
            if trading_day_index - hs_session >= mhd:
                return False
        else:
            # 兼容旧 checkpoint/直接调用方：没有交易日序号时回退到日期差。
            # 新版 V2 engine 始终传 trading_day_index，因此正式回测按交易日计数。
            hs = self.holding_since.get(c)
            if hs is None or (as_of - hs).days >= mhd:
                return False
        if sl and sl > 0:                           # 大跌豁免
            p = pnl_pct.get(c)
            if p is not None and p < -sl:
                return False
        return True

    def decide(self, ideal: dict[str, float], current_weights: dict[str, float],
               held: set[str], as_of: date,
               pnl_pct: dict[str, float] | None = None,
               risk_exit_codes: set[str] | None = None,
               protected_codes: set[str] | None = None,
               trading_day_index: int | None = None,
               ranked_codes: list[str] | None = None,
               locked_target_weights: dict[str, float] | None = None) -> dict[str, float]:
        """ideal target → actual pending_orders。

        protected_codes 只阻止普通减仓/清仓；risk_exit_codes 始终优先，确保止损和
        止盈不会被持仓锁或排名保护吞掉。locked_target_weights 是仍处于最小持有期
        的 FIFO 批次所对应的最低目标权重，防止新加仓批次被提前卖出。
        """
        drift = self.policy.rebalance_drift
        cd = self.policy.cooldown_days
        mhd = self.policy.min_holding_days
        sl = self.policy.stop_loss_pct
        max_replace = int(getattr(self.policy, "max_replace", 0) or 0)
        hard_cap = float(getattr(self.policy, "max_single_weight", 0.0) or 0.0)
        pnl = pnl_pct or {}
        risk_exits = risk_exit_codes or set()
        rank_protected = protected_codes or set()
        locked_targets = locked_target_weights or {}
        actual: dict[str, float] = {}

        # ideal 通常已经按 alpha 排名插入；显式排名使 max_replace 在所有调用方
        # 都保持确定性。退出优先级为排名最差者，不在候选集中的排在最前。
        rank = {c: i for i, c in enumerate(ranked_codes or [])}
        entry_codes = [c for c in ideal if c not in held]
        if rank:
            entry_codes.sort(key=lambda c: (rank.get(c, len(rank)), c))
        allowed_entries = (
            set(entry_codes[:max_replace]) if max_replace > 0 else set(entry_codes)
        )
        exit_codes = [c for c in held if c not in ideal]
        if rank:
            exit_codes.sort(key=lambda c: (-rank.get(c, len(rank)), c))
        else:
            exit_codes.sort()
        allowed_exits = (
            set(exit_codes[:max_replace]) if max_replace > 0 else set(exit_codes)
        )

        for c, tw in ideal.items():
            target = float(tw)
            if hard_cap > 0:
                target = min(target, hard_cap)
            if c not in held:
                if c in allowed_entries:
                    actual[c] = target              # 新建仓不受冷却/最小持仓约束
                continue
            cur_w = current_weights.get(c, 0.0)
            if c not in risk_exits:
                target = max(target, float(locked_targets.get(c, 0.0) or 0.0))
                if hard_cap > 0:
                    target = min(target, hard_cap)
            forced_cap = hard_cap > 0 and cur_w > hard_cap + 1e-12
            if forced_cap and c not in risk_exits:
                target = hard_cap
            diff = target - cur_w
            if not forced_cap and abs(diff) <= drift:
                continue                            # 偏离不足(边沿触发),不调
            if diff > 0 and cd > 0:                 # 想加仓:过冷却
                lb = self.last_buy_date.get(c)
                if lb is not None and (as_of - lb).days < cd:
                    continue
            if diff < 0 and c not in risk_exits:
                if c in rank_protected and not forced_cap:
                    continue                        # 票池前 N%:继续持有
                if not forced_cap and self._min_holding_locked(
                        c, as_of, mhd, pnl, sl, trading_day_index):
                    continue                        # 想减仓:过最小持仓(软锁)
            actual[c] = target

        for c in exit_codes:                         # 不在 ideal 的持仓:清仓/替换
            cur_w = current_weights.get(c, 0.0)
            forced_cap = hard_cap > 0 and cur_w > hard_cap + 1e-12
            if c not in allowed_exits and c not in risk_exits and not forced_cap:
                continue
            if c not in risk_exits:
                locked_target = float(locked_targets.get(c, 0.0) or 0.0)
                if locked_target > 0 and not forced_cap:
                    actual[c] = locked_target
                    continue
                if forced_cap:
                    actual[c] = hard_cap
                    continue
            if c not in risk_exits:
                if c in rank_protected:
                    continue                        # 票池前 N%:继续持有
                if self._min_holding_locked(
                        c, as_of, mhd, pnl, sl, trading_day_index):
                    continue                        # 最小持仓锁住,暂不清
            actual[c] = 0.0
        return actual

    def record_buy(self, code: str, as_of: date, was_new: bool,
                   trading_day_index: int | None = None) -> None:
        """engine 成交后回填买入状态；新建仓同时记交易日序号。"""
        self.last_buy_date[code] = as_of
        if was_new:
            self.holding_since[code] = as_of
            if trading_day_index is not None:
                self.holding_since_session[code] = trading_day_index

    def record_close(self, code: str) -> None:
        """engine 清仓后清除该 code 的换手状态。"""
        self.holding_since.pop(code, None)
        self.holding_since_session.pop(code, None)
        self.last_buy_date.pop(code, None)
