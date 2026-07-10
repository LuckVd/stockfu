"""回测引擎:虚拟账户 + T+1 开盘调仓 + 三层架构(信号/仓位/执行)。

执行时序(更接近真实交易):
  T 日收盘: AI 分析(基于 ≤T 数据) → 信号层输出 raw signal / ai_target_weight
  T 日盘后: 仓位层(PositionManager)目标仓位驱动 + 边沿触发 + 买入冷却
  T+1 开盘: 执行层按 T+1 开盘价调仓至目标仓位

核心设计:
  信号层  →  AI 输出 signal + 可选 ai_target_weight
  仓位层  →  compute_target_weight() 转为目标仓位
          →  PositionManager.should_act() 边沿触发+冷却
  执行层  →  VirtualAccount.apply_action() 整百股调仓

无未来函数:每个 as_of 只用 ≤as_of 数据(build_context 的 as_of 已保证)。
LLM 调用由调用方注入 analyze_fn(scheduler 负责 temp=0 + 并发 + 断点续跑)。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlmodel import select, and_

from stockfu.db import session_scope

INITIAL_CASH = 1_000_000.0
COMMISSION_RATE = 0.0003      # 券商佣金 万3(双边)
MIN_COMMISSION = 5.0          # 最低 5 元/笔
STAMP_DUTY_RATE = 0.0005      # 印花税 0.05%(仅卖出,2023-08 起)
TRANSFER_FEE_RATE = 0.00001   # 过户费 0.001%(双边,2022 起沪深统一)
BENCHMARK = "510300"          # 沪深300 ETF


@dataclass
class Position:
    shares: int = 0
    avg_cost: float = 0.0


class VirtualAccount:
    """虚拟账户:现金 + 持仓。借鉴 trading.recompute_holding 的移动加权平均(纯内存)。"""

    def __init__(self, initial_cash: float = INITIAL_CASH):
        self.cash: float = float(initial_cash)
        self.initial: float = float(initial_cash)
        self.positions: dict[str, Position] = {}
        self.fee_paid: float = 0.0

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + sum(
            p.shares * prices.get(c, 0.0) for c, p in self.positions.items() if p.shares > 0
        )

    def weight(self, code: str, prices: dict[str, float]) -> float:
        total = self.equity(prices)
        if total <= 0:
            return 0.0
        pos = self.positions.get(code)
        if not pos or pos.shares <= 0:
            return 0.0
        return pos.shares * prices.get(code, 0.0) / total

    def apply_action(self, code: str, action: str, target_weight: float,
                     price: float, prices: dict[str, float]) -> dict | None:
        """按 target_weight 调仓(整百股)。返回交易记录(含 realized pnl)或 None。

        买入受可用现金约束(不足则收敛到能买的整百股);卖出按目标算股数。
        action 仅用于记录语义(buy/add/reduce/sell),实际方向由 target vs current 决定。
        """
        if price <= 0 or action == "hold":
            return None
        total = self.equity(prices)
        if total <= 0:
            return None
        target_value = target_weight * total
        pos = self.positions.setdefault(code, Position())
        current_value = pos.shares * price
        delta = target_value - current_value  # 正=买,负=卖
        if abs(delta) < total * 0.001:        # 调仓量太小,不动
            return None

        if delta > 0:  # 买入
            buy_value = min(delta, self.cash)
            shares = int(buy_value / price / 100) * 100   # A 股整百股
            if shares <= 0:
                if pos.shares == 0 and self.cash >= price * 100:
                    shares = 100
                else:
                    return None
            cost = shares * price
            fee = max(cost * COMMISSION_RATE, MIN_COMMISSION) + cost * TRANSFER_FEE_RATE
            new_total = pos.shares + shares
            pos.avg_cost = (pos.avg_cost * pos.shares + cost) / new_total  # 移动加权平均
            pos.shares = new_total
            self.cash -= (cost + fee)
            self.fee_paid += fee
            return {"kind": action, "code": code, "shares": shares, "price": price,
                    "fee": round(fee, 2), "pnl": None}
        else:          # 卖出
            sell_value = -delta
            shares = int(sell_value / price / 100) * 100
            shares = min(shares, pos.shares)
            if shares <= 0:
                return None
            proceeds = shares * price
            fee = (max(proceeds * COMMISSION_RATE, MIN_COMMISSION)
                   + proceeds * (STAMP_DUTY_RATE + TRANSFER_FEE_RATE))
            realized = (price - pos.avg_cost) * shares - fee   # 已实现盈亏(扣费后,含印花税+过户费)
            pos.shares -= shares
            self.cash += (proceeds - fee)
            self.fee_paid += fee
            if pos.shares == 0:
                pos.avg_cost = 0.0
            return {"kind": action, "code": code, "shares": -shares, "price": price,
                    "fee": round(fee, 2), "pnl": round(realized, 2)}


# =====================================================================
# 内部辅助
# =====================================================================


def _get_quote_dict(codes: list[str], as_of: date, field: str = "close") -> dict[str, float]:
    """取单日单字段 → {code: value}。按 code 路由个股/ETF/指数三表(quote_model_for)。

    拆表后 510300 等 ETF 不在 quote_snapshot(已迁 etf_quote_daily)、指数在
    index_quote_daily,必须按 code 分组查对应表;codes 混合时分组后合并。
    """
    from stockfu.services.factors import quote_model_for
    groups: dict[type, list[str]] = {}
    for c in codes:
        groups.setdefault(quote_model_for(c), []).append(c)
    result: dict[str, float] = {}
    with session_scope() as s:
        for model, cs in groups.items():
            rows = s.exec(
                select(model).where(
                    and_(model.quote_date == as_of, model.asset_code.in_(cs))
                )
            ).all()
            for r in rows:
                v = getattr(r, field, None)
                if v is not None:
                    result[r.asset_code] = float(v)
    return result


def _get_trade_price(code: str, open_prices: dict[str, float],
                     close_prices: dict[str, float]) -> tuple[float, str]:
    """获取成交价:open 优先,close 兜底。返回 (price, source)。"""
    px = open_prices.get(code)
    if px is not None and px > 0:
        return px, "open"
    px = close_prices.get(code)
    if px is not None and px > 0:
        return px, "close_fallback"
    return 0.0, "unavailable"


# =====================================================================
# 绩效计算
# =====================================================================


def _metrics(equity_curve: list[dict], benchmark: list[dict],
             initial: float, days: int) -> dict:
    """算绩效:总收益/年化/最大回撤/夏普/胜率(基准对比)。"""
    import math

    eq = [p["equity"] for p in equity_curve]
    bm = [p["equity"] for p in benchmark] if benchmark else []
    out: dict = {}

    if eq and initial > 0:
        out["total_return"] = round((eq[-1] / initial - 1) * 100, 2)
        if days > 0 and eq[-1] > 0:
            out["annualized"] = round(((eq[-1] / initial) ** (252 / days) - 1) * 100, 2)
        peak, max_dd = eq[0], 0.0
        for v in eq:
            peak = max(peak, v)
            if peak > 0:
                max_dd = max(max_dd, (peak - v) / peak)
        out["max_drawdown"] = round(max_dd * 100, 2)
        rets = [(eq[i] / eq[i - 1] - 1) for i in range(1, len(eq)) if eq[i - 1] > 0]
        if len(rets) >= 5:
            mean = sum(rets) / len(rets)
            std = (sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5
            out["sharpe"] = round(mean / std * math.sqrt(252), 2) if std > 0 else 0.0
        else:
            out["sharpe"] = None

    if bm and bm[0] > 0:
        out["benchmark_return"] = round((bm[-1] / bm[0] - 1) * 100, 2)
        out["excess"] = round(out.get("total_return", 0) - out["benchmark_return"], 2)

    return out


def _benchmark_curve(code: str, days: list[date]) -> list[dict]:
    """基准(code)在 days 上的归一化净值曲线(首日=INITIAL_CASH)。

    按 code 路由对应行情表(ETF→etf_quote_daily / 指数→index_quote_daily / 个股→quote_snapshot)。
    """
    if not days:
        return []
    from stockfu.services.factors import quote_model_for
    model = quote_model_for(code)
    with session_scope() as s:
        rows = {r.quote_date: r.close for r in s.exec(select(model).where(
            model.asset_code == code, model.quote_date.in_(days))).all() if r.close}
    out, last = [], None
    for d in days:
        c = rows.get(d)
        if c:
            last = c
        if last:
            out.append({"date": d.isoformat(), "equity": last})
    if out and out[0]["equity"] > 0:
        base = out[0]["equity"]
        for p in out:
            p["equity"] = round(p["equity"] / base * INITIAL_CASH, 2)
    return out


def _trade_calendar_days(start: date, end: date) -> list[date]:
    from stockfu.services.snapshot import _trade_calendar
    cal = _trade_calendar() or []
    if not cal:
        # fallback:akshare 交易日历不可用(离线环境)时,用 quote_snapshot 历史行情日构造
        from sqlmodel import select
        from stockfu.db import session_scope
        from stockfu.models import QuoteSnapshot
        with session_scope() as s:
            cal = {d for d in s.exec(select(QuoteSnapshot.quote_date).distinct()).all() if d}
    return sorted(d for d in cal if start <= d <= end)


# =====================================================================
# 主入口
# =====================================================================


def run_backtest(codes: list[str], start: date, end: date,
                 initial_cash: float = INITIAL_CASH, analyze_fn=None,
                 max_workers: int = 4, buy_cool_down_days: int = 5,
                 max_target_step: float = 1.0,
                 risk_confirm_days: int = 1,
                 target_mode: str = "discrete",
                 max_weight: float = 0.15, total_dead: float = 3.0,
                 min_trade_weight: float = 0.0,
                 sell_cooldown_days: int = 0,
                 conf_gate: float = 0.0,
                 debounce=None) -> dict:
    """回测主循环:T+1开盘执行 + 三层架构(信号→仓位→执行)。

    每个交易日 as_of 内:
      1. 执行前日 AI 挂单(以 as_of 开盘价)
      2. 用 as_of 收盘数据跑 AI → 计算目标仓位
      3. 仓位层(PositionManager)边沿触发+买入冷却 → 挂起,次日开盘执行

    analyze_fn(code, as_of, holding_override) 默认用 ai.analyze;
    scheduler 注入带 temp=0/断点续跑缓存的版本。

    去抖旋钮(默认均为原行为;按业界 whipsaw 应对机制设计,治 5 条根因):
      buy_cool_down_days: 两次**买入**间最少交易日间隔(减仓不限)。
      max_target_step: 单次增仓目标最大上调(0-1),默认1.0;实测 0.2 帮倒忙(压仓踏空)。
      risk_confirm_days: risk 否决需连续 N 天才生效(机制1确认棒,治根因①);默认1=原行为。
      target_mode: "discrete"=阶跃查表(原);"continuous"=total 连续映射+双向滞回死区
        (机制7连续映射+机制2滞回,治根因②③=换手主因)。max_weight/total_dead 为其参数。
      min_trade_weight: 调仓幅度<此值(占总资产)不下单(机制7死区,治根因④);默认0。
      sell_cooldown_days: 部分减仓冷却天数(清仓/风险否决不限,机制4,治根因④);默认0。
      conf_gate: 弱 confidence(<此值)的清仓信号降级为维持(机制1 confidence gate,治根因⑤);默认0=关。
      debounce: StrategyDebounce(CompiledStrategy.debounce_params);传入时优先于各裸 kwargs,
                类型安全取代字符串 dict 耦合。scheduler 传它,旧调用方仍可用裸 kwargs(双入口)。
    """
    if debounce is not None:   # dataclass 覆盖各裸 kwargs(双入口向后兼容)
        buy_cool_down_days = debounce.buy_cool_down_days
        max_target_step = debounce.max_target_step
        risk_confirm_days = debounce.risk_confirm_days
        target_mode = debounce.target_mode
        max_weight = debounce.max_weight
        total_dead = debounce.total_dead
        min_trade_weight = debounce.min_trade_weight
        sell_cooldown_days = debounce.sell_cooldown_days
        conf_gate = debounce.conf_gate
    # 仓位调整层:独立基础架构,从 app_config 取(解耦于策略)
    from stockfu.ai.rebalancers import get_active_rebalancer, get_rebalancer_params
    rebalancer = get_active_rebalancer()
    rebalancer_params = get_rebalancer_params()
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from stockfu.ai.action import PositionManager, resolve_action, compute_target_weight
    from stockfu.ai.analyze import analyze as default_analyze

    days = _trade_calendar_days(start, end)
    _analyze = analyze_fn or default_analyze
    acct = VirtualAccount(initial_cash)
    pm = PositionManager(buy_cool_down_days=buy_cool_down_days,
                         max_target_step=max_target_step,
                         min_trade_weight=min_trade_weight,
                         sell_cooldown_days=sell_cooldown_days)
    _risk_streak: dict[str, int] = {}  # code → risk 连续否决天数(确认棒状态)

    equity_curve: list[dict] = []
    trades: list[dict] = []
    pending_target: dict[str, float] = {}  # {code: target_weight} 待次日开盘执行
    last_close: dict[str, float] = {}       # code → 最近有收盘价交易日的价(停牌日估值用)

    for as_of in days:
        close_prices = _get_quote_dict(codes, as_of, "close")
        if not close_prices:
            continue
        last_close.update(close_prices)   # 停牌日 close 缺失时,沿用上一交易日价估值(不记 0)

        # ---- Phase 1: 执行前日挂单(以今日开盘价;停牌无法成交的顺延次日,不丢弃) ----
        if pending_target:
            open_prices = _get_quote_dict(codes, as_of, "open")
            still_pending: dict[str, float] = {}
            for code, target_weight in list(pending_target.items()):
                # 如果 open 缺失,用 close 兜底
                if code not in open_prices and code in close_prices:
                    open_prices[code] = close_prices[code]
                px, source = _get_trade_price(code, open_prices, close_prices)
                if px <= 0:
                    # 停牌当日无法成交:保留挂单至下一个有报价的交易日(原 clear() 会永久丢信号)
                    still_pending[code] = target_weight
                    continue
                cur_w = acct.weight(code, open_prices)
                act = resolve_action(cur_w, target_weight)
                tr = acct.apply_action(code, act, target_weight, px, open_prices)
                if tr:
                    tr["date"] = as_of.isoformat()
                    tr["signal"] = None
                    tr["reason"] = "open_exec"
                    tr["price_source"] = source
                    trades.append(tr)
            pending_target = still_pending

        # ---- Phase 2: 收盘快照 + AI 分析 ----
        total0 = acct.equity(close_prices)
        cash_r = acct.cash / total0 if total0 > 0 else 0.0  # noqa: F841
        snap: dict[str, dict] = {}
        for code in close_prices:
            pos = acct.positions.get(code)
            snap[code] = {
                "holding": {"shares": pos.shares, "avg_cost": pos.avg_cost}
                           if pos and pos.shares > 0 else None,
                "weight": acct.weight(code, close_prices),
            }

        results: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            fut = {pool.submit(_analyze, c, as_of, snap[c]["holding"]): c for c in snap}
            for f in as_completed(fut):
                c = fut[f]
                try:
                    results[c] = f.result()
                except Exception:  # noqa: BLE001
                    pass

        # ---- Phase 3: 仓位层(信号→desired→组合层→目标仓位→边沿触发→冷却) ----
        # 3a. 逐标的算 desired(单标的层)+ 收集 meta(组合层排序用)
        desired: dict[str, float | None] = {}
        meta: dict[str, dict] = {}
        _sig: dict[str, str] = {}      # 记 signal/risk_vetoed 供 3c trade 记录用
        _veto: dict[str, bool] = {}
        for code in snap:
            r = results.get(code)
            if not r or "error" in r or not r.get("aggregate"):
                continue
            agg = r["aggregate"]
            signal = agg.get("final_signal", "hold")
            risk_vetoed = agg.get("risk_vetoed", False)
            ai_target = agg.get("ai_target_weight")
            total_score = agg.get("total_score")
            confidence = agg.get("confidence")
            current_w = snap[code]["weight"]

            # risk 否决确认棒:连续 N 天才生效(N=1=原行为),过滤单日抖动(头号翻转源)
            if risk_confirm_days > 1:
                if risk_vetoed:
                    _risk_streak[code] = _risk_streak.get(code, 0) + 1
                else:
                    _risk_streak[code] = 0
                risk_vetoed = _risk_streak[code] >= risk_confirm_days

            # 信号→目标仓位(discrete=阶跃查表;continuous=total 连续映射+滞回死区)
            target_weight = compute_target_weight(
                signal, risk_vetoed, current_w, ai_target,
                total_score=total_score, mode=target_mode,
                max_w=max_weight, dead=total_dead,
                targets=debounce.targets if debounce else None,
            )

            # confidence gate:弱 confidence 的清仓信号降级为维持(防 total 抖动误清仓);
            # 建仓/加仓不 gate(鼓励吃行情)。conf_gate=0 关闭。
            if (conf_gate > 0 and target_weight == 0.0 and current_w > 0
                    and confidence is not None and confidence < conf_gate):
                target_weight = None
                signal = "hold"

            desired[code] = target_weight
            _sig[code] = signal
            _veto[code] = risk_vetoed
            meta[code] = {"score": total_score, "confidence": confidence,
                          "signal": signal, "risk_vetoed": risk_vetoed}

        # 3b. 仓位调整层:desired全集 + current全集 → 最终目标仓位(独立基础架构,从 app_config 取)
        current_weights = {c: s["weight"] for c, s in snap.items()}   # 全集(含未覆盖持仓)
        final = rebalancer.adjust(
            desired, current_weights, meta,
            equity=acct.equity(last_close),
            params=rebalancer_params,
        )

        # 3c. 边沿触发 + 冷却(遍历 final 全集;未覆盖维持的 code 过 should_act 是 no-op)
        for code, target_weight in final.items():
            current_w = current_weights[code]
            should, target, reason = pm.should_act(
                code, target_weight, current_w, as_of, days,
            )
            if should:
                pending_target[code] = target
                trades.append({
                    "date": as_of.isoformat(),
                    "code": code,
                    "signal": _sig.get(code),
                    "risk_vetoed": _veto.get(code),
                    "target_weight": round(target, 4) if target is not None else None,
                    "reason": reason,
                    "status": "pending",
                })

        # ---- Record: 收盘净值(停牌持仓用 last_close 估值,不记 0) ----
        equity_curve.append({
            "date": as_of.isoformat(),
            "equity": round(acct.equity(last_close), 2),
        })

    # ---- 绩效 ----
    benchmark = _benchmark_curve(BENCHMARK, days)
    filled = [t for t in trades if t.get("status") != "pending"]
    win = [t for t in filled if t.get("pnl") is not None and t["pnl"] > 0]
    loss = [t for t in filled if t.get("pnl") is not None and t["pnl"] <= 0]

    metrics = _metrics(equity_curve, benchmark, initial_cash, len(days))
    metrics["trade_count"] = len(filled)
    metrics["win_rate"] = round(len(win) / (len(win) + len(loss)) * 100, 1) if (win or loss) else None
    metrics["total_fee"] = round(acct.fee_paid, 2)
    metrics["final_equity"] = round(
        acct.equity(last_close) if last_close else initial_cash, 2
    )
    metrics["config"] = {
        "buy_cool_down_days": buy_cool_down_days,
        "max_target_step": max_target_step,
        "risk_confirm_days": risk_confirm_days,
        "target_mode": target_mode,
        "max_weight": max_weight,
        "total_dead": total_dead,
        "min_trade_weight": min_trade_weight,
        "sell_cooldown_days": sell_cooldown_days,
        "conf_gate": conf_gate,
        "execution": "T+1_open",
        "rebalancer": rebalancer.rebalancer_id,
    }

    return {
        "equity_curve": equity_curve,
        "benchmark": benchmark,
        "trades": trades,
        "metrics": metrics,
        "codes": list(codes),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "initial_cash": initial_cash,
        "days": len(days),
    }
