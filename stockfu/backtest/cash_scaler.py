"""现金预算缩放:买单等比缩放到可用现金。

借鉴 rqalpha `order_target_portfolio_smart` 的思路(用一个标量全局缩放所有买单,
而不是逐笔抢现金),解决 engine 旧版 `min(delta, cash)` 逐笔夹断丢目标的痛点:

  - 逐笔夹断(旧):先到先得,排后面的买单买不足直接 `return None`,
    目标权重永久丢失,且结果依赖执行序(谁先谁拿到钱)。
  - 等比缩放(新):卖单先释放现金,再对所有买单用一个 `safety` 标量等比缩放,
    使 Σ(买单成本) + 估算费用 ≤ 可用现金。缩放后的目标传给 apply_action,
    其内部 `min(delta, cash)` 基本不再触发(仅整百股取整的微小残留由它兜底)。
    **不丢任何标的的目标,与执行序无关。**

费用纳入:`need = gross_cost + est_fee`,从源头避免"满仓必超现金 → 触发夹断"。
这是 StockFu 评估对标 rqalpha/zipline/backtrader 后落地的执行层资金分配方案
(详见 docs/ARCHITECTURE_REVIEW.md)。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from stockfu.backtest.engine import VirtualAccount


def scale_buys_to_cash(
    acct: "VirtualAccount",
    buys: list[tuple[str, float, float]],
    prices: dict[str, float],
    *,
    commission_rate: float,
    transfer_fee_rate: float,
    min_commission: float,
) -> tuple[list[tuple[str, float, float]], float, bool]:
    """等比缩放买单到可用现金。

    acct:   VirtualAccount(用 .cash / .equity(prices) / .weight(code, prices))
    buys:   [(code, target_weight, price)] —— 均为 target > current 的买单(调用方已分向)
    prices: {code: price}
    返回 (scaled, safety, constrained):
      scaled       = [(code, scaled_target_weight, price)] 保持原顺序
      safety ∈[0,1]= 缩放系数(1.0 = 现金充足无需缩放);缩放的是"增量"(target-cur)
      constrained  = safety < 1.0,当日是否触发现金预算约束(供 metrics 计数)
    """
    if not buys:
        return [], 1.0, False

    total = acct.equity(prices)
    if total <= 0 or acct.cash <= 0:
        # 无可用现金:所有买单退回当前权重(= 当日不买,但不静默——constrained=True)
        return ([(code, acct.weight(code, prices), price) for code, _, price in buys],
                0.0, True)

    # 每个买单的增量价值 (target - current) × 总资产
    deltas: list[tuple[str, float, float, float, float]] = []
    for code, tw, price in buys:
        cur_w = acct.weight(code, prices)
        d = (tw - cur_w) * total
        if d > 0 and price > 0:
            deltas.append((code, tw, cur_w, d, price))
    if not deltas:
        return [(c, tw, p) for c, tw, p in buys], 1.0, False

    gross_cost = sum(d for _, _, _, d, _ in deltas)
    # 买入端费用估算:佣金(每笔最低 min_commission)+ 过户费。按总额估,保守留余量,
    # 防止"满仓超现金"在 apply_action 内触发夹断。
    est_fee = gross_cost * (commission_rate + transfer_fee_rate) + min_commission * len(deltas)
    need = gross_cost + est_fee

    if need <= acct.cash:
        safety = 1.0
    else:
        safety = max(0.0, acct.cash / need) if need > 0 else 0.0

    constrained = safety < 1.0
    scaled = [(code, cur_w + (tw - cur_w) * safety, price)
              for code, tw, cur_w, _d, price in deltas]
    return scaled, safety, constrained
