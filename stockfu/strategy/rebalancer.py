"""换手抑制层(有状态 Rebalancer,组合执行控制)。

插在 portfolio/risk 产出的「理想目标」与 engine 下单的「实际订单」之间:给定 ideal
target + 当前持仓 + 持仓状态(建仓日/最近买入日)+ 浮亏,按组合政策的旋钮决定「今天
实际调哪些」:
  - rebalance_drift :偏离阈值(边沿触发),|目标-当前|≤此值不调 → 抑制机械再平衡
  - cooldown_days   :买入冷却,建仓/加仓后 N 日内不再加仓
  - min_holding_days:最小持仓(软锁),建仓后 N 日内不卖/不清仓——但浮亏 ≥ stop_loss_pct
                     时豁免(该止损能止损,不扛回撤)
  - stop_loss_pct   :大跌豁免阈值,浮亏 ≥ 此值解除 min_holding 锁(配套软锁)
四者默认 0/None = 关闭,decide 退化为「全量目标 + 清仓」,等价旧 engine 行为。

只控换手(执行质量),不改 strategy_score、不改理想选股(§4 不变量)。
浮亏由 engine 用 Position.avg_cost + 当日收盘价算好传入(pnl_pct),rebalancer 不记成本。
状态(holding_since/last_buy_date)由 engine 在成交后经 record_buy/record_close 回填。
"""
from __future__ import annotations

from datetime import date


class Rebalancer:
    def __init__(self, policy) -> None:
        self.policy = policy
        self.holding_since: dict[str, date] = {}    # code -> 建仓日(首次买入)
        self.last_buy_date: dict[str, date] = {}    # code -> 最近一次买入日

    def _min_holding_locked(self, c: str, as_of: date, mhd: int,
                            pnl_pct: dict[str, float], sl) -> bool:
        """min_holding 是否锁住 c:锁定期内 且 未触发大跌豁免。"""
        if mhd <= 0:
            return False
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
               pnl_pct: dict[str, float] | None = None) -> dict[str, float]:
        """ideal target → actual pending_orders(偏离阈值 + 冷却 + 最小持仓[软锁])。"""
        drift = self.policy.rebalance_drift
        cd = self.policy.cooldown_days
        mhd = self.policy.min_holding_days
        sl = self.policy.stop_loss_pct
        pnl = pnl_pct or {}
        actual: dict[str, float] = {}

        for c, tw in ideal.items():
            if c not in held:
                actual[c] = tw                      # 新建仓:买入不受冷却/最小持仓约束
                continue
            cur_w = current_weights.get(c, 0.0)
            diff = tw - cur_w
            if abs(diff) <= drift:
                continue                            # 偏离不足(边沿触发),不调
            if diff > 0 and cd > 0:                 # 想加仓:过冷却
                lb = self.last_buy_date.get(c)
                if lb is not None and (as_of - lb).days < cd:
                    continue
            if diff < 0 and self._min_holding_locked(c, as_of, mhd, pnl, sl):
                continue                            # 想减仓:过最小持仓(软锁,大跌豁免)
            actual[c] = tw

        for c in held:                              # 不在 ideal 的持仓:清仓(过最小持仓软锁)
            if c in ideal:
                continue
            if self._min_holding_locked(c, as_of, mhd, pnl, sl):
                continue                            # 最小持仓锁住(且未大跌),暂不清
            actual[c] = 0.0
        return actual

    def record_buy(self, code: str, as_of: date, was_new: bool) -> None:
        """engine 成交后回填:任何买入刷新 last_buy_date;新建仓记 holding_since。"""
        self.last_buy_date[code] = as_of
        if was_new:
            self.holding_since[code] = as_of

    def record_close(self, code: str) -> None:
        """engine 清仓后清除该 code 的换手状态。"""
        self.holding_since.pop(code, None)
        self.last_buy_date.pop(code, None)
