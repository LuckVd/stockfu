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
最小手:空仓开仓按 100 股计入预算,与 apply_action 建仓特例对齐,避免「scaler 以为
买得起 → 实际抢现金序相关」。

返回长度始终 == len(buys)、按 code 可索引(engine 勿 zip 错位)。
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

    acct:   VirtualAccount(用 .cash / .equity(prices) / .weight(code, prices) / .positions)
    buys:   [(code, target_weight, price)] —— 均为 target > current 的买单(调用方已分向)
    prices: {code: price}
    返回 (scaled, safety, constrained):
      scaled       = [(code, scaled_target_weight, price)] **与 buys 等长同序**
      safety ∈[0,1]= 缩放系数(1.0 = 现金充足无需缩放);缩放的是"增量"(target-cur)
      constrained  = safety < 1.0
    """
    if not buys:
        return [], 1.0, False

    total = acct.equity(prices)
    if total <= 0 or acct.cash <= 0:
        # 无可用现金:所有买单退回当前权重(= 当日不买,但不静默——constrained=True)
        return ([(code, acct.weight(code, prices), price) for code, _, price in buys],
                0.0, True)

    # 每个买单:预算名义(含空仓最小 1 手) + 原始目标
    # entries: (code, tw, cur_w, budget_notional, price)
    entries: list[tuple[str, float, float, float, float]] = []
    for code, tw, price in buys:
        cur_w = acct.weight(code, prices)
        d = (tw - cur_w) * total
        if price <= 0 or d <= 0:
            # 无实际增量:保持原 tw(或 cur),不参与缩放池
            entries.append((code, tw, cur_w, 0.0, price))
            continue
        pos = acct.positions.get(code)
        cur_shares = pos.shares if pos else 0
        shares = int(d / price / 100) * 100
        if shares <= 0 and cur_shares == 0:
            # 与 apply_action 建仓特例对齐:预算按 1 手计
            shares = 100
        if shares <= 0:
            entries.append((code, tw, cur_w, 0.0, price))
            continue
        notional = shares * price
        entries.append((code, tw, cur_w, notional, price))

    active = [e for e in entries if e[3] > 0]
    if not active:
        return [(c, tw, p) for c, tw, p in buys], 1.0, False

    gross_cost = sum(e[3] for e in active)
    est_fee = (gross_cost * (commission_rate + transfer_fee_rate)
               + min_commission * len(active))
    need = gross_cost + est_fee

    if need <= acct.cash:
        safety = 1.0
    else:
        safety = max(0.0, acct.cash / need) if need > 0 else 0.0

    constrained = safety < 1.0
    # 等长同序:有预算的缩放增量;无预算的(d<=0)退回 cur_w 以免误加仓
    scaled: list[tuple[str, float, float]] = []
    for code, tw, cur_w, notional, price in entries:
        if notional > 0:
            new_tw = cur_w + (tw - cur_w) * safety
            scaled.append((code, new_tw, price))
        else:
            scaled.append((code, cur_w, price))
    return scaled, safety, constrained
