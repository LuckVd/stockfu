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
from stockfu.backtest.cash_scaler import scale_buys_to_cash

INITIAL_CASH = 1_000_000.0
COMMISSION_RATE = 0.0003      # 券商佣金 万3(双边)
MIN_COMMISSION = 5.0          # 最低 5 元/笔
STAMP_DUTY_RATE = 0.0005      # 印花税 0.05%(仅卖出,2023-08 起)
TRANSFER_FEE_RATE = 0.00001   # 过户费 0.001%(双边,2022 起沪深统一)
BENCHMARK = "sh000001"        # 上证综指（回测基准，1990 起）

# 资金分配 / 风控默认值(对标 rqalpha order_target_portfolio_smart + backtrader Margin 思路,
# 详见 docs/ARCHITECTURE_REVIEW.md):
#   - 总仓安全阀留 cash sleeve,保证 Σ目标 ≤ max_gross → 执行层现金够、不夹断丢目标
#   - 规则止损补文档承诺(旧 BACKTEST.md 写"-3%止损"但代码缺失;此处参数化,A股 -3% 太敏感)
DEFAULT_MAX_GROSS = 0.90      # Σ目标权重上限(留 10% 现金;对所有 rebalancer 生效)
DEFAULT_STOP_LOSS = 0.08      # 个股成本止损:浮亏达此比例 → 强制清仓
DEFAULT_PORTFOLIO_BRAKE = 0.10  # 组合回撤刹车:equity 较峰值回撤达此值 → 全局临时降仓一半


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
                # 建仓特例:目标增量不足 100 股但现金够 1 手(+费用)时建最小仓。
                # 预检必须纳入费用 —— 旧版只判 price*100,扣 cost+fee 后 cash 会落到约 -5 元。
                est_cost = price * 100
                est_fee = max(est_cost * COMMISSION_RATE, MIN_COMMISSION) + est_cost * TRANSFER_FEE_RATE
                if pos.shares == 0 and self.cash >= est_cost + est_fee:
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
    """取单日单字段 → {code: value}，个股回测信号路径用。

    注：quote_model_for 当前为单表(一律 QuoteSnapshot——G01 拆表已回滚，见 G02 OQ4)，
    故此处实际只查 quote_snapshot；ETF/指数不在该表。回测基准(_benchmark_curve)
    单独直读 IndexQuoteDaily，不经此函数。
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


def _apply_gross_cap(final: dict[str, float | None], max_gross: float) -> dict[str, float | None]:
    """总仓位安全阀:若 Σ正值权重 > max_gross,等比缩放所有正值权重到 Σ=max_gross。

    max_gross >= 1.0 或无正值 → 原样返回(不限制)。留 cash sleeve = 1 - max_gross。
    """
    if max_gross >= 1.0:
        return final
    gross = sum(w for w in final.values() if w)
    if gross <= max_gross or gross <= 0:
        return final
    factor = max_gross / gross
    return {c: (w * factor if w else w) for c, w in final.items()}


# =====================================================================
# 绩效计算
# =====================================================================


def _metrics(equity_curve: list[dict], benchmark: list[dict],
             initial: float, days: int,
             bench_window: dict | None = None) -> dict:
    """算绩效:总收益/年化/最大回撤/夏普/胜率(基准对比)。

    bench_window: {"start","end"} 基准实际可用窗口。excess 按交集算:
    取 equity_curve 在基准窗口内的子段,与该窗口的 benchmark_return 对比。
    """
    import math

    eq = [p["equity"] for p in equity_curve]
    bm = [p["equity"] for p in benchmark] if benchmark else []
    out: dict = {}

    total_r = None
    if eq and initial > 0:
        total_r = (eq[-1] / initial - 1) * 100
        out["total_return"] = round(total_r, 2)
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
            # sortino:仅用下行波动(负收益),衡量"坏波动"风险调整收益
            downside = [r for r in rets if r < 0]
            if len(downside) >= 2:
                dstd = (sum(r * r for r in downside) / (len(downside) - 1)) ** 0.5
                out["sortino"] = round(mean / dstd * math.sqrt(252), 2) if dstd > 0 else 0.0
            else:
                out["sortino"] = None
            # calmar:年化收益 / 最大回撤(风险调整,越大越好)
            mdd = out.get("max_drawdown")
            if out.get("annualized") is not None and mdd and mdd > 0:
                out["calmar"] = round(out["annualized"] / mdd, 2)
        else:
            out["sharpe"] = None
            out["sortino"] = None

    # 基准:按交集窗口算 excess（total_return 已在上方设置，此处只引用，不重算）
    out["benchmark_window"] = bench_window
    if bm and bm[0] > 0:
        out["benchmark_return"] = round((bm[-1] / bm[0] - 1) * 100, 2)
        out["excess"] = round((out.get("total_return") or 0.0) - out["benchmark_return"], 2)
    else:
        out["benchmark_return"] = None
        out["excess"] = None
        out["benchmark_reason"] = "无数据（index_quote_daily 无对应区间数据）"

    return out


def _benchmark_curve(code: str, days: list[date]) -> tuple[list[dict], dict | None]:
    """基准(code)在 days 上的归一化净值曲线(首日=INITIAL_CASH)，返回 (曲线, 窗口信息)。

    直读 IndexQuoteDaily（不走 quote_model_for，指数独立表）。
    窗口信息 = {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"} 或 None（无数据）。
    交集截断：早于基准首日的 days 不产出曲线点，由调用方按交集算 excess。
    """
    if not days:
        return [], None
    from stockfu.models import IndexQuoteDaily
    with session_scope() as s:
        rows = {r.quote_date: r.close for r in s.exec(
            select(IndexQuoteDaily).where(
                IndexQuoteDaily.asset_code == code,
                IndexQuoteDaily.quote_date >= min(days),
                IndexQuoteDaily.quote_date <= max(days),
            )).all() if r.close}
    if not rows:
        return [], None
    sorted_dates = sorted(rows.keys())
    window = {"start": sorted_dates[0].isoformat(), "end": sorted_dates[-1].isoformat()}
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
    return out, window


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
                 prefetch_fn=None,
                 max_workers: int = 4, buy_cool_down_days: int = 5,
                 max_target_step: float = 1.0,
                 risk_confirm_days: int = 1,
                 target_mode: str = "discrete",
                 max_weight: float = 0.15, total_dead: float = 3.0,
                 min_trade_weight: float = 0.0,
                 sell_cooldown_days: int = 0,
                 conf_gate: float = 0.0,
                 debounce=None,
                 max_gross: float = DEFAULT_MAX_GROSS,
                 stop_loss_pct: float = DEFAULT_STOP_LOSS,
                 portfolio_brake_dd: float = DEFAULT_PORTFOLIO_BRAKE) -> dict:
    """回测主循环:T+1开盘执行 + 三层架构(信号→仓位→执行)。

    每个交易日 as_of 内:
      1. 执行前日 AI 挂单(以 as_of 开盘价)
      2. 用 as_of 收盘数据跑 AI → 计算目标仓位
      3. 仓位层(PositionManager)边沿触发+买入冷却 → 挂起,次日开盘执行

    analyze_fn(code, as_of, holding_override[, cache_prefill]) 默认用 ai.analyze;
    scheduler 注入带 temp=0/断点续跑缓存的版本。prefetch_fn(codes, as_of) 可选:Phase 2
    前单日批量预读算子缓存 → 注入 analyze 的 cache_prefill(跳过逐次 get_operator_result
    往返);为 None 时退回原路径。analyze_fn 须能接 cache_prefill(第 4 参)才用预读。

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
        # 资金分配/风控:yaml risk 段可选配置(StrategyDebounce 字段 None=未配,用 engine 默认)
        _v = getattr(debounce, "max_gross", None)
        if _v is not None: max_gross = _v
        _v = getattr(debounce, "stop_loss_pct", None)
        if _v is not None: stop_loss_pct = _v
        _v = getattr(debounce, "portfolio_brake_dd", None)
        if _v is not None: portfolio_brake_dd = _v
    # 仓位调整层:独立基础架构,从 app_config 取(解耦于策略)
    from stockfu.ai.rebalancers import get_active_rebalancer, get_rebalancer_params
    rebalancer = get_active_rebalancer()
    rebalancer_params = get_rebalancer_params()
    # max_gross 优先级:app_config rebalancer_params > yaml debounce > 默认。让 cap_and_rank
    # 内部竞争额度与 engine 层安全阀用同一值,避免 pass_through/top_n_picker 不限仓导致现金被吃光。
    _mp = rebalancer_params.get("max_gross")
    if _mp is not None:
        max_gross = float(_mp)
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
    holdings_curve: list[dict] = []          # 每日逐票持仓快照(完整持仓记录,供直观回看)
    trades: list[dict] = []
    pending_target: dict[str, float] = {}  # {code: target_weight} 待次日开盘执行
    last_close: dict[str, float] = {}       # code → 最近有收盘价交易日的价(停牌日估值用)
    peak_equity: float = float(initial_cash)  # 组合回撤刹车:追踪回测内权益峰值
    cash_constraint_hits: int = 0             # 当日买单触发现金缩放的天数(可观测,对标 backtrader Margin)

    for as_of in days:
        close_prices = _get_quote_dict(codes, as_of, "close")
        if not close_prices:
            continue
        last_close.update(close_prices)   # 停牌日 close 缺失时,沿用上一交易日价估值(不记 0)

        # ---- Phase 1: 执行前日挂单(T+1 开盘价;停牌顺延次日不丢弃)----
        # 先卖后买 + 买单等比缩放到可用现金(对标 rqalpha order_target_portfolio_smart):
        #   卖单先成交释放现金 → 买单再用释放后的现金;买单总额 > 现金时用 safety 标量等比
        #   缩放,不逐笔 min(delta,cash) 夹断丢目标。各方向内部按 code 排序保跨进程可复现。
        if pending_target:
            open_prices = _get_quote_dict(codes, as_of, "open")
            still_pending: dict[str, float] = {}
            sells: list[tuple[str, float, float, str]] = []   # (code, target_weight, px, source)
            buys: list[tuple[str, float, float, str]] = []
            for code, target_weight in sorted(pending_target.items()):
                if code not in open_prices and code in close_prices:
                    open_prices[code] = close_prices[code]
                px, source = _get_trade_price(code, open_prices, close_prices)
                if px <= 0:
                    still_pending[code] = target_weight       # 停牌顺延,不丢信号
                    continue
                act = resolve_action(acct.weight(code, open_prices), target_weight)
                if act in ("sell", "reduce"):
                    sells.append((code, target_weight, px, source))
                elif act in ("buy", "add"):
                    buys.append((code, target_weight, px, source))
                # act == "hold":差额过小,跳过(apply_action 内也会过滤碎单)

            def _exec(code, tw, px, source, **extra):
                tr = acct.apply_action(code, resolve_action(acct.weight(code, open_prices), tw),
                                        tw, px, open_prices)
                if tr:
                    tr.update(date=as_of.isoformat(), signal=None, reason="open_exec",
                              price_source=source, **extra)
                    trades.append(tr)

            # 1a. 先执行所有卖单(按 code 序)——释放现金给买单
            for code, tw, px, source in sells:
                _exec(code, tw, px, source)
            # 1b. 买单等比缩放到可用现金(卖单释放后),再执行(按 code 序)
            scaled, safety, constrained = scale_buys_to_cash(
                acct, [(c, tw, px) for c, tw, px, _ in buys], open_prices,
                commission_rate=COMMISSION_RATE, transfer_fee_rate=TRANSFER_FEE_RATE,
                min_commission=MIN_COMMISSION)
            if constrained:
                cash_constraint_hits += 1
            for (code, _tw, px, source), (_c, scaled_tw, _p) in zip(buys, scaled):
                _exec(code, scaled_tw, px, source,
                      **({"cash_scaled": round(safety, 4)} if constrained else {}))
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
        # 单日批量预读缓存:一次 SELECT 取回当日全部 (code,算子) 命中,注入各 analyze。
        # 跳过逐 (code,as_of,算子) 的 get_operator_result 往返 —— 大样本主力提速。
        # prefill=None(无 prefetch_fn)时退回原 3 参调用,向后兼容默认 ai.analyze 路径。
        prefill = prefetch_fn(list(snap.keys()), as_of) if prefetch_fn else None
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            if prefill is not None:
                fut = {pool.submit(_analyze, c, as_of, snap[c]["holding"], prefill): c for c in snap}
            else:
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

            # 个股成本止损(规则化风控,补 BACKTEST.md 承诺但缺失的代码):浮亏达 stop_loss → 强制清仓。
            # stop_loss_pct=0 关闭;仅对持仓且策略想持有/加仓(target>0)时介入,不与已清仓重复。
            if stop_loss_pct > 0 and current_w > 0 and target_weight not in (0.0, None):
                _pos = acct.positions.get(code)
                _px = close_prices.get(code, 0.0)
                if (_pos and _pos.shares > 0 and _pos.avg_cost > 0 and _px > 0
                        and _px / _pos.avg_cost - 1 <= -stop_loss_pct):
                    target_weight = 0.0
                    signal = "stop_loss"

            desired[code] = target_weight
            _sig[code] = signal
            _veto[code] = risk_vetoed
            meta[code] = {"score": total_score, "confidence": confidence,
                          "signal": signal, "risk_vetoed": risk_vetoed,
                          "raw": agg.get("total_raw", total_score)}

        # 3b. 仓位调整层:desired全集 + current全集 → 最终目标仓位(独立基础架构,从 app_config 取)
        current_weights = {c: s["weight"] for c, s in snap.items()}   # 全集(含未覆盖持仓)
        final = rebalancer.adjust(
            desired, current_weights, meta,
            equity=acct.equity(last_close),
            params=rebalancer_params,
        )

        # 组合回撤刹车(规则化风控):equity 较回测峰值回撤达阈值 → 全局临时降仓一半(风险优先)。
        if portfolio_brake_dd > 0:
            _cur_eq = acct.equity(last_close)
            peak_equity = max(peak_equity, _cur_eq)
            if peak_equity > 0 and _cur_eq / peak_equity - 1 <= -portfolio_brake_dd:
                final = {c: (w * 0.5 if w else w) for c, w in final.items()}
        # 总仓安全阀:Σ目标权重 ≤ max_gross(留 1-max_gross 现金,对所有 rebalancer 生效)→
        # 保证执行层买单总额 ≤ 可投资现金,不夹断丢目标。超限等比缩放所有正值权重。
        final = _apply_gross_cap(final, max_gross)

        # 3c. 边沿触发 + 冷却(遍历 final 全集;未覆盖维持的 code 过 should_act 是 no-op)
        # sorted by code:final 经 rebalancer 的 set 构造、顺序随哈希随机化漂移;
        # 排序后挂单入 pending_target 的序确定 → 次日 Phase 1 执行序确定(见上)。
        for code, target_weight in sorted(final.items()):
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
        # ---- Record: 逐票持仓快照(完整持仓记录;停牌持仓用 last_close 估值,不漏) ----
        eq_total = acct.equity(last_close)
        day_pos = []
        for c, p in acct.positions.items():
            if p.shares <= 0:
                continue
            px = close_prices.get(c) or last_close.get(c, 0.0)
            mv = p.shares * px
            day_pos.append({
                "code": c,
                "shares": p.shares,
                "avg_cost": round(p.avg_cost, 4),
                "close": round(px, 4),
                "mkt_val": round(mv, 2),
                "pnl": round(mv - p.shares * p.avg_cost, 2),   # 浮动盈亏(未扣费)
                "weight": round(mv / eq_total, 4) if eq_total > 0 else 0.0,
            })
        day_pos.sort(key=lambda x: -x["mkt_val"])
        holdings_curve.append({
            "date": as_of.isoformat(),
            "cash": round(acct.cash, 2),
            "equity": round(eq_total, 2),
            "positions": day_pos,
        })

    # ---- 绩效 ----
    benchmark, bench_window = _benchmark_curve(BENCHMARK, days)
    filled = [t for t in trades if t.get("status") != "pending"]
    win = [t for t in filled if t.get("pnl") is not None and t["pnl"] > 0]
    loss = [t for t in filled if t.get("pnl") is not None and t["pnl"] <= 0]

    metrics = _metrics(equity_curve, benchmark, initial_cash, len(days),
                        bench_window=bench_window)
    metrics["trade_count"] = len(filled)
    metrics["win_rate"] = round(len(win) / (len(win) + len(loss)) * 100, 1) if (win or loss) else None
    metrics["total_fee"] = round(acct.fee_paid, 2)
    # 组合层指标(从 holdings_curve 算,对标 zipline ledger gross leverage + 单仓集中度):
    _gross = [sum(p["weight"] for p in d.get("positions", [])) for d in holdings_curve]
    metrics["avg_gross_leverage"] = round(sum(_gross) / len(_gross) * 100, 1) if _gross else None
    metrics["max_gross_leverage"] = round(max(_gross) * 100, 1) if _gross else None
    metrics["max_single_weight"] = round(
        max((p["weight"] for d in holdings_curve for p in d.get("positions", [])), default=0.0) * 100, 1)
    metrics["cash_constraint_hits"] = cash_constraint_hits   # 买单被现金缩放的天数(可观测)
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
        "max_gross": max_gross,
        "stop_loss_pct": stop_loss_pct,
        "portfolio_brake_dd": portfolio_brake_dd,
        "execution": "T+1_open_sell_first",
        "rebalancer": rebalancer.rebalancer_id,
    }

    return {
        "equity_curve": equity_curve,
        "holdings_curve": holdings_curve,
        "benchmark": benchmark,
        "trades": trades,
        "metrics": metrics,
        "codes": list(codes),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "initial_cash": initial_cash,
        "days": len(days),
    }
