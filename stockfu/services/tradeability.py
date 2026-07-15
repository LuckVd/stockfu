"""天级可成交检查:停牌 / 涨跌停近似 / 固定滑点。

原则:
  - 只用当日 OHLC + pct_chg + is_st + board(无未来信息)
  - 前复权序列下用 pct_chg 顶格 + OHLC 粘合近似涨跌停(允许边界误差,禁止偷看)
  - 滑点默认略保守;可关(slip_bps=0)做旧行为对照
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from stockfu.services.universe import limit_pct_for


@dataclass
class ExecutionRules:
    """执行假设(写入 metrics.config,可审计)。"""
    limit_rule: bool = True
    slip_bps: float = 10.0
    on_unfillable: str = "defer"          # defer | cancel
    limit_tol: float = 0.15               # pct_chg 距满幅度容差(百分点)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FillDecision:
    ok: bool
    price: float                          # 调整后成交价(含滑点);ok=False 时为原价
    reason: str                           # ok / suspended / limit_up_no_buy / ...
    status: str                           # filled / rejected / deferred


def _near_equal(a: float, b: float, rel: float = 0.002) -> bool:
    if a <= 0 or b <= 0:
        return False
    return abs(a - b) / max(a, b) <= rel


def is_limit_locked(
    side: str,
    pct_chg: float | None,
    open_: float | None,
    high: float | None,
    low: float | None,
    close: float | None,
    board: str = "main",
    is_st: bool = False,
    tol: float = 0.15,
) -> str | None:
    """若因涨跌停不可成交,返回 reason;否则 None。

    side: "buy" | "sell"
    一字/封死近似: |pct| 接近满幅度 且 OHLC 近似粘合。
    """
    if pct_chg is None:
        return None
    lim = limit_pct_for(board, is_st=is_st)
    # 涨停买不进
    if side == "buy" and pct_chg >= lim - tol:
        if open_ and high and low and close:
            if (_near_equal(open_, high) and _near_equal(high, low)
                    and _near_equal(low, close)):
                return "limit_up_no_buy"
        elif pct_chg >= lim - 0.05:
            # 无完整 OHLC 时:顶格涨幅保守拒绝买单
            return "limit_up_no_buy"
    # 跌停卖不出
    if side == "sell" and pct_chg <= -(lim - tol):
        if open_ and high and low and close:
            if (_near_equal(open_, high) and _near_equal(high, low)
                    and _near_equal(low, close)):
                return "limit_down_no_sell"
        elif pct_chg <= -(lim - 0.05):
            return "limit_down_no_sell"
    return None


def apply_slip(price: float, side: str, slip_bps: float) -> float:
    if price <= 0 or slip_bps <= 0:
        return price
    frac = slip_bps / 10000.0
    if side == "buy":
        return price * (1.0 + frac)
    return price * (1.0 - frac)


def check_fill(
    side: str,
    price: float,
    *,
    pct_chg: float | None = None,
    open_: float | None = None,
    high: float | None = None,
    low: float | None = None,
    close: float | None = None,
    board: str = "main",
    is_st: bool = False,
    trade_status: int = 1,
    rules: ExecutionRules | None = None,
) -> FillDecision:
    """判决能否按 price 方向成交;返回调整价与 reason。"""
    rules = rules or ExecutionRules()
    if price <= 0:
        st = "deferred" if rules.on_unfillable == "defer" else "rejected"
        return FillDecision(False, price, "no_price", st)
    if trade_status == 0:
        st = "deferred" if rules.on_unfillable == "defer" else "rejected"
        return FillDecision(False, price, "suspended", st)

    if rules.limit_rule:
        why = is_limit_locked(
            side, pct_chg, open_, high, low, close,
            board=board, is_st=is_st, tol=rules.limit_tol,
        )
        if why:
            st = "deferred" if rules.on_unfillable == "defer" else "rejected"
            return FillDecision(False, price, why, st)

    px = apply_slip(price, side, rules.slip_bps)
    return FillDecision(True, px, "ok", "filled")
