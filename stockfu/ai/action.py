"""AI 动作化 + 仓位管理器。

三层架构:
  1. 信号层 — AI 输出 raw signal + 可选 ai_target_weight
  2. 仓位层 — PositionManager(目标仓位驱动,边沿触发+买入冷却)
  3. 执行层 — VirtualAccount.apply_action 按目标权重调仓

规则化(不调 LLM):保证回测可复现 + 省成本。回测与实盘共用同一决策层。
"""
from __future__ import annotations

from datetime import date
from typing import Optional

# 信号 → 默认目标仓位占比(0-1)。hold=None 表示"维持当前仓位"。
# PositionManager 不直接使用此表——由调用方计算目标仓位后传入。
_SIGNAL_TARGET: dict[str, Optional[float]] = {
    "strong_buy": 0.10,
    "buy": 0.06,
    "hold": None,        # 维持
    "sell": 0.0,
    "strong_sell": 0.0,
}

_WEIGHT_EPS = 0.005


# =====================================================================
# 仓位管理器（目标仓位驱动）
# =====================================================================


class PositionManager:
    """仓位管理器(目标仓位驱动 + 边沿触发 + 买入冷却)。

    核心思路:不再比较 signal zone,而是直接比较目标仓位数值。
    - 目标仓位变化 → 触发交易(自然支持渐进加减仓)
    - 买入冷却:仅限制增仓方向的频率,减仓/清仓无冷却
    - 卖出/减仓永远优先(风险控制)

    状态追踪:
      _last_executed[code] = 上次执行时的目标仓位
      _last_buy[code]      = 上次买入日期(用于冷却计算)
    """

    def __init__(self, buy_cool_down_days: int = 5, *,
                 edge_threshold: float = 0.005,
                 sell_cooldown_days: int = 0,
                 min_trade_weight: float = 0.0,
                 max_target_step: float = 1.0):
        """
        buy_cool_down_days: 增仓冷却(交易日)。默认5。
        edge_threshold: 边沿触发阈值,目标仓位变化(相对上次执行)超此才动。默认0.005(=原行为)。
        sell_cooldown_days: 部分减仓冷却(交易日);清仓(target=0/风险否决)永不冷却(风险优先)。默认0。
        min_trade_weight: 最低调仓幅度(|target-current|占总资产),低于不动(过滤整百股碎单)。默认0。
        max_target_step: 单次增仓目标最大上调幅度,避免一次性重仓(如0→70%)。默认1.0(不限)。
        """
        self.buy_cool_down_days = buy_cool_down_days
        self.sell_cooldown_days = sell_cooldown_days
        self.edge_threshold = edge_threshold
        self.min_trade_weight = min_trade_weight
        self.max_target_step = max_target_step
        self._last_executed: dict[str, float] = {}  # code → last traded target
        self._last_buy: dict[str, date] = {}         # code → last buy date
        self._last_sell: dict[str, date] = {}        # code → last sell date

    # -----------------------------------------------------------------
    # 公开接口
    # -----------------------------------------------------------------

    def should_act(self, code: str, target_weight: float | None,
                   current_weight: float, as_of: date,
                   trade_calendar: list[date]) -> tuple[bool, float, str]:
        """边沿触发决策。返回 (should_act, target_weight, reason)。

        target_weight: 外部已确定的目标仓位(含 risk_veto/ai/规则映射)
        current_weight: 当前实际仓位
        """
        if target_weight is None:
            return False, current_weight, "no_target"

        executed = self._last_executed.get(code, current_weight)

        # 限制单次增仓幅度(避免一次性重仓,如0→70%);减仓方向不限(风险优先)
        if self.max_target_step < 1.0 and target_weight > executed:
            target_weight = min(target_weight, executed + self.max_target_step)

        # 调仓幅度过小(占总资产)→ 过滤碎单
        min_w = max(_WEIGHT_EPS, self.min_trade_weight)
        if abs(current_weight - target_weight) < min_w:
            self._last_executed[code] = target_weight
            return False, current_weight, "below_min_trade"

        # Edge Trigger: 目标仓位与上次执行差超阈值
        edge = max(_WEIGHT_EPS, self.edge_threshold)
        if abs(target_weight - executed) < edge:
            return False, target_weight, "edge: below threshold"

        # 冷却(增仓/减仓分别)
        if target_weight > current_weight:            # 增仓
            if self.buy_cool_down_days > 0:
                last = self._last_buy.get(code)
                if last is not None:
                    gap = [d for d in trade_calendar if last < d <= as_of]
                    if len(gap) < self.buy_cool_down_days:
                        return False, target_weight, (
                            f"buy_cooldown: {len(gap)}/{self.buy_cool_down_days}d")
        elif target_weight < current_weight:          # 减仓
            # 清仓(target<=0,含风险一票否决)永不冷却,立即执行(风险优先);
            # 仅部分减仓(target>0)限频,过滤隔日噪音调仓
            if self.sell_cooldown_days > 0 and target_weight > 0:
                last = self._last_sell.get(code)
                if last is not None:
                    gap = [d for d in trade_calendar if last < d <= as_of]
                    if len(gap) < self.sell_cooldown_days:
                        return False, target_weight, (
                            f"sell_cooldown: {len(gap)}/{self.sell_cooldown_days}d")

        # 通过
        self._last_executed[code] = target_weight
        if target_weight > current_weight:
            self._last_buy[code] = as_of
        elif target_weight < current_weight:
            self._last_sell[code] = as_of

        direction = "increase" if target_weight > current_weight else "decrease"
        return True, target_weight, f"trade: {executed:.0%}→{target_weight:.0%} ({direction})"

    def reset(self):
        """重置状态(用于新回测)。"""
        self._last_executed.clear()
        self._last_buy.clear()
        self._last_sell.clear()


# =====================================================================
# 执行层辅助
# =====================================================================


def resolve_action(current_weight: float, target_weight: float) -> str:
    """比较当前与目标仓位 → 执行动作语义。"""
    if target_weight is None:
        return "hold"
    if abs(current_weight - target_weight) < _WEIGHT_EPS:
        return "hold"
    if current_weight <= 0 and target_weight > 0:
        return "buy"
    if current_weight > 0 and target_weight <= 0:
        return "sell"
    if target_weight > current_weight:
        return "add"
    if target_weight < current_weight:
        return "reduce"
    return "hold"


def _total_to_weight(total: float | None, max_w: float = 0.15,
                     dead: float = 3.0) -> float | None:
    """total_score → 目标仓位 连续映射(替代 _SIGNAL_TARGET 阶跃查表)。

    消除阈值穿越抖动 + 内建双向滞回死区(业界机制7连续映射 + 机制2滞回):
      total <= -dead   → 0.0(清仓)
      -dead < t < dead → None(死区,维持当前)  ← 双向滞回带,治 buy/sell 阈值横跳
      total >= +dead   → 线性增到 max_w(total=+20 满仓)
    例:max_w=0.15,dead=3 → total=+5→3.75% / +10→7.5% / +20→15% / ∈(-3,3)→维持 / ≤-3→清仓。
    """
    if total is None:
        return None
    if total <= -dead:
        return 0.0
    if total < dead:
        return None  # 死区,维持(调用方收到 None 不动)
    return round(max_w * min(total / 20.0, 1.0), 4)


def compute_target_weight(signal: str, risk_vetoed: bool,
                          current_weight: float,
                          ai_target_weight: float | None = None,
                          total_score: float | None = None,
                          mode: str = "discrete", max_w: float = 0.15,
                          dead: float = 3.0,
                          targets: dict | None = None) -> float | None:
    """计算���标仓位(信号层→仓位层的桥梁)。

    mode="discrete": risk_veto > ai_target > targets(策略YAML) > _SIGNAL_TARGET(框架默认)。
    mode="continuous": 用 total_score 连续映射(忽略 ai_target,规则化可复现、无阶跃抖动、
      内建滞回死区)。治根因②③(阈值无滞回 + 信号→仓位阶跃映射)。

    targets: 策略 YAML position.targets 传入的仓位映射表，优先于硬编码 _SIGNAL_TARGET。
    """
    if risk_vetoed:
        return 0.0
    if mode == "continuous" and total_score is not None:
        return _total_to_weight(total_score, max_w, dead)
    if ai_target_weight is not None:
        return ai_target_weight
    table = targets if targets else _SIGNAL_TARGET
    tw = table.get(signal)
    return tw if tw is not None else current_weight


# =====================================================================
# 旧接口(保留兼容,不再被回测使用)
# =====================================================================


def decide_action(final_signal: str, total_score: float, risk_vetoed: bool,
                  current_weight: float, cash_ratio: float | None = None) -> dict:
    """规则化仓位决策(旧接口,回测已改用 PositionManager)。"""
    sig = final_signal or "hold"
    sc = f"(评分 {total_score:+.0f})" if total_score is not None else ""

    if risk_vetoed:
        if current_weight > 0:
            return {"action": "sell", "target_weight": 0.0,
                    "reason": f"风险顾问一票否决{sc},清仓(当前 {current_weight:.0%})"}
        return {"action": "hold", "target_weight": 0.0,
                "reason": f"风险顾问一票否决{sc},无仓位,观望"}

    target = _SIGNAL_TARGET.get(sig)

    if target is None:
        if current_weight > 0:
            return {"action": "hold", "target_weight": current_weight,
                    "reason": f"信号 {sig}{sc},维持 {current_weight:.0%}"}
        return {"action": "hold", "target_weight": 0.0,
                "reason": f"信号 {sig}{sc},无仓位,观望"}

    if target <= 0:
        if current_weight > 0:
            return {"action": "sell", "target_weight": 0.0,
                    "reason": f"信号 {sig}{sc},清仓"}
        return {"action": "hold", "target_weight": 0.0,
                "reason": f"信号 {sig}{sc},无仓位,观望"}

    act = resolve_action(current_weight, target)
    cash_note = f", 可用资金 {cash_ratio:.0%}" if cash_ratio is not None and act == "buy" else ""
    return {"action": act, "target_weight": target,
            "reason": f"信号 {sig}{sc},{act} {current_weight:.0%}→{target:.0%}{cash_note}"}
