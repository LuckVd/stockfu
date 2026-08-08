"""V2 风险覆盖层。

风险层只修改目标敞口，不修改 strategy_score。实现的是 V1 已验证的风险语义，
而不是复制 V1 的逐股票编排：固定止损、分段/硬止盈、组合回撤刹车、市场趋势
regime、波动率目标和总敞口上限。所有有路径依赖的状态都可 checkpoint/resume。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from stockfu.scoring.contracts import fingerprint


@dataclass(frozen=True)
class TakeProfitTier:
    """profit 达标后，在 trailing drawdown 条件满足时卖出一部分仓位。"""

    profit: float
    drawdown: float = 0.0
    sell_fraction: float = 1.0

    def to_dict(self) -> dict[str, float]:
        return {
            "profit": self.profit,
            "drawdown": self.drawdown,
            "sell_fraction": self.sell_fraction,
        }


@dataclass(frozen=True)
class RiskPolicy:
    risk_policy_id: str
    version: int
    stop_loss: float | None = None
    take_profit: float | None = None
    take_profit_tiers: tuple[TakeProfitTier, ...] = ()
    take_profit_hard_pct: float | None = None
    drawdown_brake: float | None = None
    drawdown_brake_scale: float = 0.50
    drawdown_brake_max_gross: float | None = None
    drawdown_brake_tiers: tuple[tuple[float, float], ...] = ()
    drawdown_brake_mode: str = "scale_all"
    drawdown_recover_dd: float | None = None
    drawdown_recover_high_days: int = 0
    market_regime_code: str | None = None
    market_regime_ma_days: int | None = None
    market_regime_enter_band: float = 0.0
    market_regime_exit_band: float = 0.03
    market_regime_max_gross: float = 0.50
    volatility_target: float | None = None
    volatility_window: int = 63
    volatility_floor: float = 0.30
    max_gross: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "risk_policy_id": self.risk_policy_id,
            "version": self.version,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "take_profit_tiers": [t.to_dict() for t in self.take_profit_tiers],
            "take_profit_hard_pct": self.take_profit_hard_pct,
            "drawdown_brake": self.drawdown_brake,
            "drawdown_brake_scale": self.drawdown_brake_scale,
            "drawdown_brake_max_gross": self.drawdown_brake_max_gross,
            "drawdown_brake_tiers": [list(t) for t in self.drawdown_brake_tiers],
            "drawdown_brake_mode": self.drawdown_brake_mode,
            "drawdown_recover_dd": self.drawdown_recover_dd,
            "drawdown_recover_high_days": self.drawdown_recover_high_days,
            "market_regime_code": self.market_regime_code,
            "market_regime_ma_days": self.market_regime_ma_days,
            "market_regime_enter_band": self.market_regime_enter_band,
            "market_regime_exit_band": self.market_regime_exit_band,
            "market_regime_max_gross": self.market_regime_max_gross,
            "volatility_target": self.volatility_target,
            "volatility_window": self.volatility_window,
            "volatility_floor": self.volatility_floor,
            "max_gross": self.max_gross,
        }

    def fingerprint(self) -> str:
        return fingerprint(self.to_dict(), prefix="risk")


def _tier(raw: Any) -> TakeProfitTier:
    if isinstance(raw, dict):
        return TakeProfitTier(
            profit=float(raw["profit"]),
            drawdown=float(raw.get("drawdown", 0.0)),
            sell_fraction=float(raw.get("sell_fraction", 1.0)),
        )
    values = list(raw)
    if len(values) < 1:
        raise ValueError("take_profit_tiers 的每一档至少需要 profit")
    return TakeProfitTier(
        profit=float(values[0]),
        drawdown=float(values[1]) if len(values) > 1 else 0.0,
        sell_fraction=float(values[2]) if len(values) > 2 else 1.0,
    )


def risk_from_dict(d: dict[str, Any]) -> RiskPolicy:
    regime = d.get("market_regime") or {}
    tiers = tuple(_tier(v) for v in (d.get("take_profit_tiers") or ()))
    mode = str(d.get("drawdown_brake_mode", "scale_all"))
    if mode not in ("scale_all", "block_new_buys"):
        raise ValueError("drawdown_brake_mode 必须是 scale_all 或 block_new_buys")
    for t in tiers:
        if t.profit < 0 or t.drawdown < 0 or not 0 < t.sell_fraction <= 1:
            raise ValueError(f"止盈档位参数非法: {t}")
    brake_tiers_raw = d.get("drawdown_brake_tiers", d.get("portfolio_brake_tiers")) or ()
    brake_tiers: list[tuple[float, float]] = []
    for raw in brake_tiers_raw:
        if isinstance(raw, dict):
            dd, cap = raw.get("drawdown"), raw.get("max_gross")
        else:
            values = list(raw)
            if len(values) < 2:
                raise ValueError("drawdown_brake_tiers 每档需要 [drawdown, max_gross]")
            dd, cap = values[0], values[1]
        dd, cap = float(dd), float(cap)
        if dd < 0 or cap < 0:
            raise ValueError("drawdown_brake_tiers 的 drawdown/max_gross 不得为负")
        brake_tiers.append((dd, cap))
    brake_tiers.sort(key=lambda x: x[0])
    brake_dd = d.get("drawdown_brake", d.get("portfolio_brake_dd"))
    brake_scale = d.get("drawdown_brake_scale", d.get("portfolio_brake_scale", 0.50))
    brake_max = d.get("drawdown_brake_max_gross", d.get("portfolio_brake_max_gross"))
    recover_dd = d.get("drawdown_recover_dd", d.get("portfolio_brake_recover_dd"))
    recover_high = d.get(
        "drawdown_recover_high_days",
        d.get("portfolio_brake_recover_high_days", 0),
    )
    volatility_target = d.get(
        "volatility_target",
        d.get("market_regime_target_vol", regime.get("target_vol")),
    )
    volatility_window = d.get(
        "volatility_window",
        d.get("market_regime_vol_window", regime.get("vol_window", 63)),
    )
    volatility_floor = d.get(
        "volatility_floor",
        d.get("market_regime_vol_floor", regime.get("vol_floor", 0.30)),
    )
    return RiskPolicy(
        risk_policy_id=str(d["risk_policy_id"]), version=int(d["version"]),
        stop_loss=d.get("stop_loss"), take_profit=d.get("take_profit"),
        take_profit_tiers=tiers,
        take_profit_hard_pct=d.get("take_profit_hard_pct"),
        drawdown_brake=brake_dd,
        drawdown_brake_scale=float(brake_scale),
        drawdown_brake_max_gross=brake_max,
        drawdown_brake_tiers=tuple(brake_tiers),
        drawdown_brake_mode=mode,
        drawdown_recover_dd=recover_dd,
        drawdown_recover_high_days=max(int(recover_high or 0), 0),
        market_regime_code=d.get("market_regime_code", regime.get("code")),
        market_regime_ma_days=d.get("market_regime_ma_days", regime.get("ma_days")),
        market_regime_enter_band=float(d.get("market_regime_enter_band", regime.get("enter_band", 0.0))),
        market_regime_exit_band=float(d.get("market_regime_exit_band", regime.get("exit_band", 0.03))),
        market_regime_max_gross=float(d.get("market_regime_max_gross", regime.get("max_gross", 0.50))),
        volatility_target=volatility_target,
        volatility_window=int(volatility_window),
        volatility_floor=float(volatility_floor),
        max_gross=d.get("max_gross"),
    )


class RiskOverlay:
    """V1-inspired, stateful risk overlay for V2 target weights."""

    def __init__(self, policy: RiskPolicy) -> None:
        self.policy = policy
        self.peak_equity: float = 0.0
        self.bear_latched: bool = False
        self.brake_latched: bool = False
        self.equity_window: list[float] = []
        self.forced_exit_codes: set[str] = set()
        # 仅记录本次 apply 触发强制退出的原因；不参与风险决策。
        # 引擎用它把止损、硬止盈、分档追踪止盈区分开，避免只看到一个 code 集合。
        self.forced_exit_reasons: dict[str, str] = {}
        self.trigger_counts: dict[str, int] = {
            "stop_loss": 0, "take_profit": 0, "drawdown_brake": 0,
            "market_regime": 0, "volatility_target": 0,
        }
        # 当前调用是否实际修改了理想目标。引擎用它区分“风险需要日级调仓”和
        # “普通组合目标只在 rebalance 日调整”。不要把 risk 输出再次作为下一日输入。
        self.last_adjusted: bool = False

    def _cap(self, target_weights: dict[str, float], cap: float | None) -> dict[str, float]:
        if cap is None:
            return dict(target_weights)
        if cap <= 0:
            return {c: (0.0 if w and w > 0 else w)
                    for c, w in target_weights.items()}
        total = sum(w for w in target_weights.values() if w and w > 0)
        if total <= cap:
            return dict(target_weights)
        factor = cap / total
        return {c: (w * factor if w and w > 0 else w)
                for c, w in target_weights.items()}

    def _apply_brake(self, target_weights: dict[str, float], current: dict[str, float],
                     cap: float | None) -> dict[str, float]:
        p = self.policy
        if p.drawdown_brake_mode == "block_new_buys":
            out = {
                c: (current.get(c, 0.0) if w > current.get(c, 0.0) else w)
                for c, w in target_weights.items()
            }
        else:
            # V1 允许 scale>1 的回撤加仓变体；总仓 cap 仍是最后安全阀。
            scale = min(max(p.drawdown_brake_scale, 0.0), 1.5)
            out = {
                c: (w * scale if w and w > 0 else w)
                for c, w in target_weights.items()
            }
        return self._cap(out, cap)

    def _brake_cap(self, drawdown: float) -> float | None:
        """按 V1 的回撤深度档位取最严的组合敞口上限。"""
        p = self.policy
        cap = p.drawdown_brake_max_gross
        for threshold, tier_cap in p.drawdown_brake_tiers:
            if drawdown + 1e-12 >= threshold:
                cap = tier_cap if cap is None else min(cap, tier_cap)
            else:
                break
        return cap

    def _brake_active(self, equity: float) -> tuple[bool, float | None]:
        """更新组合刹车状态，支持即时触发、恢复回撤和滚动新高释放。"""
        p = self.policy
        if self.peak_equity <= 0:
            return False, None
        drawdown = max(0.0, 1.0 - equity / self.peak_equity)
        threshold = float(p.drawdown_brake or 0.0)
        tier_threshold = p.drawdown_brake_tiers[0][0] if p.drawdown_brake_tiers else None
        trigger_threshold = threshold if threshold > 0 else (tier_threshold or 0.0)
        below = trigger_threshold > 0 and drawdown + 1e-12 >= trigger_threshold
        if below:
            self.brake_latched = True
        elif self.brake_latched:
            if p.drawdown_recover_high_days > 0:
                reference = max(self.equity_window) if self.equity_window else 0.0
                if equity >= reference:
                    self.brake_latched = False
            elif p.drawdown_recover_dd is not None:
                if drawdown < float(p.drawdown_recover_dd):
                    self.brake_latched = False
            else:
                self.brake_latched = False
        active = below or self.brake_latched
        if p.drawdown_recover_high_days > 0:
            self.equity_window.append(equity)
            self.equity_window = self.equity_window[-p.drawdown_recover_high_days:]
        else:
            self.equity_window = []
        return active, self._brake_cap(drawdown) if active else None

    def _market_cap(self, benchmark_closes: list[float]) -> float | None:
        p = self.policy
        cap = p.max_gross
        if p.market_regime_ma_days and p.market_regime_ma_days > 0:
            n = p.market_regime_ma_days
            if len(benchmark_closes) >= max(5, n // 4):
                window = benchmark_closes[-min(n, len(benchmark_closes)):]
                px = window[-1]
                ma = sum(window) / len(window) if window else 0.0
                if not self.bear_latched and px < ma * (1.0 - p.market_regime_enter_band):
                    self.bear_latched = True
                elif self.bear_latched and px > ma * (1.0 + p.market_regime_exit_band):
                    self.bear_latched = False
                if self.bear_latched:
                    cap = p.market_regime_max_gross if cap is None else min(cap, p.market_regime_max_gross)
                    self.trigger_counts["market_regime"] += 1
        if p.volatility_target and p.volatility_target > 0:
            n = max(int(p.volatility_window), 2)
            if len(benchmark_closes) > n:
                seg = benchmark_closes[-(n + 1):]
                returns = [seg[i] / seg[i - 1] - 1.0
                           for i in range(1, len(seg)) if seg[i - 1] > 0]
                if len(returns) >= max(10, n // 2):
                    mean = sum(returns) / len(returns)
                    variance = sum((r - mean) ** 2 for r in returns) / len(returns)
                    realized = variance ** 0.5 * (252.0 ** 0.5)
                    if realized > 0:
                        scale = max(min(1.0, p.volatility_target / realized), p.volatility_floor)
                        vol_cap = (p.max_gross or 1.0) * scale
                        cap = vol_cap if cap is None else min(cap, vol_cap)
                        if scale < 1.0:
                            self.trigger_counts["volatility_target"] += 1
        return cap

    def apply(self, target_weights: dict[str, float], account, prices: dict[str, float],
              as_of, *, execution_prices: dict[str, float] | None = None,
              benchmark_closes: list[float] | None = None) -> dict[str, float]:
        """应用风险规则；``prices`` 用于估值，``execution_prices`` 用于盈亏判断。"""
        p = self.policy
        source_target = dict(target_weights)
        out = dict(source_target)
        self.last_adjusted = False
        self.forced_exit_codes = set()
        self.forced_exit_reasons = {}
        if execution_prices is None:
            execution_prices = prices
        equity = account.equity(prices)
        self.peak_equity = max(self.peak_equity, equity)
        current = {c: account.weight(c, prices) for c in account.positions
                   if account.positions[c].shares > 0}

        for code, pos in account.positions.items():
            if pos.shares <= 0:
                continue
            px = execution_prices.get(code, 0.0)
            if px <= 0 or pos.avg_cost <= 0:
                continue
            pos.peak_close = max(pos.peak_close, px)
            gain = px / pos.avg_cost - 1.0
            if p.stop_loss and gain <= -float(p.stop_loss) + 1e-12:
                out[code] = 0.0
                self.forced_exit_codes.add(code)
                self.forced_exit_reasons[code] = "stop_loss"
                self.trigger_counts["stop_loss"] += 1
                continue
            if p.take_profit and gain >= float(p.take_profit) - 1e-12:
                out[code] = 0.0
                self.forced_exit_codes.add(code)
                self.forced_exit_reasons[code] = "take_profit"
                self.trigger_counts["take_profit"] += 1
                continue

            if p.take_profit_hard_pct and gain >= p.take_profit_hard_pct - 1e-12:
                out[code] = 0.0
                self.forced_exit_codes.add(code)
                self.forced_exit_reasons[code] = "take_profit_hard"
                self.trigger_counts["take_profit"] += 1
                continue

            if p.take_profit_tiers:
                peak_gain = pos.peak_close / pos.avg_cost - 1.0 if pos.avg_cost > 0 else 0.0
                # 与 V1 helper 一致：同日同时跨过多档时先处理收益门槛最高的一档。
                ordered_tiers = sorted(
                    enumerate(p.take_profit_tiers),
                    key=lambda item: (-item[1].profit, -item[1].drawdown, item[0]),
                )
                for idx, tier in ordered_tiers:
                    key = f"take_profit_trailing_{tier.profit:g}_{tier.drawdown:g}"
                    trailing_hit = (
                        peak_gain + 1e-12 >= tier.profit
                        and (tier.drawdown <= 0 or
                             px / pos.peak_close - 1.0 <= -tier.drawdown + 1e-12)
                    )
                    if not trailing_hit or key in pos.take_profit_fired:
                        continue
                    if pos.take_profit_anchor_shares <= 0:
                        pos.take_profit_anchor_shares = pos.shares
                    pos.take_profit_fired.add(key)
                    anchor = pos.take_profit_anchor_shares
                    remaining = max(0.0, 1.0 - sum(
                        t.sell_fraction for j, t in enumerate(p.take_profit_tiers)
                        if f"take_profit_trailing_{t.profit:g}_{t.drawdown:g}" in pos.take_profit_fired
                    ))
                    cap_shares = int(anchor * remaining) // 100 * 100
                    if remaining > 0 and cap_shares <= 0:
                        cap_shares = min(100, pos.shares)
                    pos.take_profit_cap_shares = (
                        cap_shares if pos.take_profit_cap_shares is None
                        else min(pos.take_profit_cap_shares, cap_shares)
                    )
                    # 分档止盈也是风险强制减仓，不能被 portfolio 的持仓软锁或
                    # 排名保护拦截；下方 cap_weight 会再次确认本次确实需要卖出。
                    self.forced_exit_codes.add(code)
                    self.forced_exit_reasons[code] = key
                    self.trigger_counts["take_profit"] += 1
                    break

            if pos.take_profit_cap_shares is not None and code in out and equity > 0:
                cap_value = (
                    pos.take_profit_cap_shares + pos.receivable_shares
                ) * prices.get(code, 0.0)
                cap_weight = cap_value / equity
                out[code] = min(out[code], cap_weight)
                if out[code] < current.get(code, 0.0) - 1e-12:
                    self.forced_exit_codes.add(code)
                    self.forced_exit_reasons.setdefault(
                        code, "take_profit_trailing_cap")

        if p.drawdown_brake or p.drawdown_brake_tiers:
            active, cap = self._brake_active(equity)
            if active:
                out = self._apply_brake(out, current, cap)
                self.trigger_counts["drawdown_brake"] += 1

        if benchmark_closes:
            out = self._cap(out, self._market_cap(benchmark_closes))
        else:
            out = self._cap(out, p.max_gross)
        # 缩放、强制退出、止盈上限或 regime/vol cap 任一改变目标时，允许引擎
        # 在非组合调仓日执行风险调整；没有风险变化时不得每天重新平衡组合。
        keys = set(source_target) | set(out)
        self.last_adjusted = any(
            abs(float(source_target.get(c, 0.0)) - float(out.get(c, 0.0))) > 1e-12
            for c in keys
        )
        return out

    def checkpoint_state(self) -> dict[str, Any]:
        return {
            "peak_equity": self.peak_equity,
            "bear_latched": self.bear_latched,
            "brake_latched": self.brake_latched,
            "equity_window": list(self.equity_window),
            "trigger_counts": dict(self.trigger_counts),
            "forced_exit_reasons": dict(self.forced_exit_reasons),
        }

    def restore_state(self, data: dict[str, Any]) -> None:
        self.peak_equity = float(data.get("peak_equity", 0.0))
        self.bear_latched = bool(data.get("bear_latched", False))
        self.brake_latched = bool(data.get("brake_latched", False))
        self.equity_window = [float(v) for v in (data.get("equity_window") or [])]
        restored = {k: int(v) for k, v in (data.get("trigger_counts") or {}).items()}
        self.trigger_counts = {**self.trigger_counts, **restored}
        self.forced_exit_reasons = {
            str(code): str(reason)
            for code, reason in (data.get("forced_exit_reasons") or {}).items()
        }

    def metrics(self) -> dict[str, int]:
        return {f"risk_{k}_count": int(v) for k, v in self.trigger_counts.items()}
