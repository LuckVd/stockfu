"""AI 动作化 + 仓位管理器。

三层架构:
  1. 信号层 — AI 输出 raw signal + 可选 ai_target_weight
  2. 仓位层 — PositionManager(目标仓位驱动,边沿触发+买入冷却)
  3. 执行层 — VirtualAccount.apply_action 按目标权重调仓

规则化(不调 LLM):保证回测可复现 + 省成本。回测与实盘共用同一决策层。
"""
from __future__ import annotations

from datetime import date

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
                     dead: float = 3.0, score_full: float = 20.0,
                     total_sell: float | None = None,
                     held: bool = False) -> float | None:
    """total_score → 目标仓位 连续映射(替代 _SIGNAL_TARGET 阶跃查表)。

    两种模式:
    - 旧路径(total_sell 未配):对称滞回死区(业界机制7连续映射 + 机制2滞回):
        total <= -dead   → 0.0(清仓)
        -dead < t < dead → None(死区,维持当前)
        total >= +dead   → 线性增到 max_w(total=score_full 满仓)
    - 双总分路径(total_sell 配置,归一化 ±100 刻度):买卖不对称滞回——空仓用
      total(买入总分)判定建仓线 +dead;持仓用 total_sell(卖出总分)判定清仓线
      -dead,买入总分 ≥ dead 仍可继续加仓,否则维持。避免持仓后分数小降即被清。
    """
    if total is None:
        return None
    if total_sell is not None:
        # 买卖不对称滞回(归一化刻度):建仓看 total,清仓看 total_sell。
        if held:
            if total_sell <= -dead:
                return 0.0
            if total < dead:
                return None  # 维持:卖出分未破线、买入分不足加仓
            return round(max_w * min(total / score_full, 1.0), 4)
        if total < dead:
            return None  # 空仓:买入分不足建仓
        return round(max_w * min(total / score_full, 1.0), 4)
    if total <= -dead:
        return 0.0
    if total < dead:
        return None  # 死区,维持(调用方收到 None 不动)
    return round(max_w * min(total / score_full, 1.0), 4)


def compute_target_weight(risk_vetoed: bool,
                          current_weight: float,
                          ai_target_weight: float | None = None,
                          total_score: float | None = None,
                          total_sell_score: float | None = None,
                          max_w: float = 0.15, dead: float = 3.0,
                          score_full: float = 20.0) -> float | None:
    """计算目标仓位(信号层→仓位层的桥梁)。

    G10 后统一连续映射(铲除 ±20/signal 阶跃体系):
      risk_vetoed                → 0(一票否决清仓)
      total_score is not None    → _total_to_weight 连续映射(满仓刻度 score_full→max_w)
      否则                       → 透传 ai_target_weight(或 None=维持)

    total_sell_score: 卖出总分(买入/卖出权重不对称时配置,归一化 ±100 刻度)。
      设置后进入买卖不对称滞回:持仓 current_weight>0 用 total_sell_score 判定清仓线。

    score_full: 满仓刻度(total_score≥此值→满仓 max_w);按算子集量纲配,默认 20。
    """
    if risk_vetoed:
        return 0.0
    if total_score is not None:
        return _total_to_weight(total_score, max_w, dead, score_full,
                                total_sell=total_sell_score,
                                held=current_weight > 0)
    return ai_target_weight if ai_target_weight is not None else None


