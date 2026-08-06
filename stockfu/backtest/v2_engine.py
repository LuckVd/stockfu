"""V2 回测引擎(设计 §9、§14、§15)。

逐日批量编排,严格时间协议:
    预热期 [history_origin, eval_start)   :只算 raw + 更新历史状态,不评分不交易
    观察期 eval_dates 的前 1/5             :评分(observation=True)但 no-trade
    formal  后 4/5                         :评分 + rebalance 日产生 t+1 订单

每日顺序(§9.3,硬约束):
    1. 执行 t-1 产生的待执行订单(成交时点可见数据)
    2. 解析 t 日点时 universe;评分只读 cutoff < t 的历史状态
    3. 批量算 raw → factor score(同一状态为全部股票评分)
    4. alpha 聚合 → (观察期跳过)组合+risk → t+1 待执行订单
    5. **所有评分完成后**才把 t 日观测追加进历史状态

记账/撮合/分红/费用复用 engine.py 已验证单元(§3.3);估值 qfq、credit_dividends=False
(qfq 已含分红再投)。本引擎只重写评分编排,不沾 V1 per-code analyze + score_full。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

from stockfu.backtest.engine import (
    BENCHMARK,
    COMMISSION_RATE,
    INITIAL_CASH,
    MIN_COMMISSION,
    TRANSFER_FEE_RATE,
    VirtualAccount,
    _backtest_series_ctx,
    _get_day_market,
    _get_trade_price,
    _metrics,
    _preload_dividend_events,
    _preload_market_range,
    _preload_cash_dividends,
    _preload_stock_dividends,
    _trade_calendar_days,
    settle_dividends,
)
from stockfu.backtest.cash_scaler import scale_buys_to_cash
from stockfu.scoring.contracts import (
    RawFactorObservation,
    fingerprint,
)
from stockfu.scoring.history import HistoryState, compute_sample_dates
from stockfu.scoring.scorer import FactorScorer
from stockfu.scoring.profiles import FactorProfile
from stockfu.strategy.alpha import AlphaAggregator, AlphaDefinition
from stockfu.strategy.portfolio import DayContext, PortfolioConstructor
from stockfu.strategy.rebalancer import Rebalancer
from stockfu.strategy.risk import RiskOverlay
from stockfu.services.tradeability import ExecutionRules, check_fill, infer_pre_close
from stockfu.services.universe import DayFlags, UniverseContext, UniverseRules

_PRELOAD_LOOKBACK_DAYS = 1900      # 覆盖 raw 最大回看(low_vol ~年级)
_COMP_SHORT = {"self_history": "self", "market_history": "market",
               "industry_history": "industry"}   # history_specs 名 → update 短名


# ----------------------------------------------------------- 配置与结果


@dataclass
class V2RunConfig:
    alpha: AlphaDefinition
    portfolio: PortfolioConstructor
    risk: RiskOverlay
    profiles: dict[str, FactorProfile]            # profile_id -> FactorProfile
    raw_computers: dict[str, callable]            # raw_metric_id -> (code, as_of)->RawFactorObservation
    codes: list[str]
    eval_start: date
    eval_end: date
    history_origin: date
    initial_cash: float = INITIAL_CASH
    market_scope: str = "cn_equity"
    benchmark_code: str = BENCHMARK
    valuation_basis: str = "qfq"
    credit_dividends: bool = False                # qfq 已含分红再投
    observation_count: int | None = None          # None→ceil(0.2·eval);固定则 prefix invariant(§9.4)

    def manifest(self, **extra) -> dict:
        base = {
            "alpha_fingerprint": self.alpha.fingerprint(),
            "portfolio_fingerprint": self.portfolio.policy.fingerprint(),
            "risk_fingerprint": self.risk.policy.fingerprint(),
            "profile_fingerprints": {pid: p.mapping_fingerprint() for pid, p in self.profiles.items()},
            "codes_count": len(self.codes),
            "eval_start": self.eval_start.isoformat(),
            "eval_end": self.eval_end.isoformat(),
            "history_origin": self.history_origin.isoformat(),
            "initial_cash": self.initial_cash,
            "market_scope": self.market_scope,
            "benchmark_code": self.benchmark_code,
            "valuation_basis": self.valuation_basis,
            "credit_dividends": self.credit_dividends,
            "observation_count": self.observation_count,
        }
        base.update(extra)
        return base


@dataclass
class V2Result:
    metrics: dict
    equity_curve: list[dict]                      # 全期(含预热/观察)
    formal_equity_curve: list[dict]               # 仅 formal 期
    benchmark: list[dict]                         # formal 期归一基准
    trades: list[dict]
    manifest: dict
    history_checkpoint: dict
    observation_summary: dict
    formal_summary: dict
    first_trade_date: date | None = None
    last_trade_date: date | None = None


# ----------------------------------------------------------- 辅助


def _load_listing_and_industry(codes: list[str]) -> tuple[dict, dict]:
    """一次性查 stock_basic 的上市日与行业(点时近似:用当前分类,见 v2-notes §0.4)。"""
    from sqlalchemy import text
    from stockfu.db import engine as db_engine

    listing: dict[str, date] = {}
    industry: dict[str, str | None] = {}
    codes = list(codes)
    with db_engine.connect() as conn:
        for i in range(0, len(codes), 500):
            chunk = codes[i:i + 500]
            ph = ",".join(f":c{j}" for j in range(len(chunk)))
            params = {f"c{j}": chunk[j] for j in range(len(chunk))}
            rows = conn.execute(text(
                f"select code, listing_date, industry from stock_basic "
                f"where code in ({ph})"), params).all()
            for r in rows:
                ld = r[1]
                listing[r[0]] = date.fromisoformat(ld) if ld else None
                industry[r[0]] = r[2]
    return listing, industry


def _amount_20d(sctx, code: str, as_of: date, window: int = 20) -> float:
    di = sctx.date_idx.get(as_of)
    if di is None:
        return 0.0
    cols = sctx.series.get(code)
    if cols is None:
        return 0.0
    arr = cols.get("amt")
    if arr is None:
        return 0.0
    lo = max(0, di - window + 1)
    vals = [arr[i] for i in range(lo, di + 1) if not math.isnan(arr[i])]
    return sum(vals) / len(vals) if vals else 0.0


def _classify(current_w: float, target_w: float) -> str:
    if target_w <= 0.0 and current_w > 0.0:
        return "sell"
    if target_w < current_w - 0.001:
        return "reduce"
    if target_w > current_w + 0.001:
        return "add" if current_w > 0.0 else "buy"
    return "hold"


# ----------------------------------------------------------- 主入口


def run_v2_backtest(cfg: V2RunConfig) -> V2Result:
    alpha = cfg.alpha
    portfolio = cfg.portfolio
    risk = cfg.risk
    profiles = cfg.profiles
    raw_computers = cfg.raw_computers
    codes = list(cfg.codes)
    bench = cfg.benchmark_code

    # profile_id -> raw_metric_id
    pid_to_metric = {pid: p.raw_metric_id for pid, p in profiles.items()}
    alpha_profile_ids = [f.profile_id for f in alpha.factors]

    # 交易日历 + 预载(含 benchmark)
    dates_all = _trade_calendar_days(cfg.history_origin, cfg.eval_end)
    eval_dates = [d for d in dates_all if d >= cfg.eval_start]
    if len(eval_dates) < 5:
        raise ValueError(f"eval_dates 过少({len(eval_dates)})")
    if cfg.observation_count is not None:
        # 固定观察期:延长 eval_end 不改 formal_start(§9.4 prefix invariance)
        obs_count = min(cfg.observation_count, max(0, len(eval_dates) - 1))
    else:
        obs_count = math.ceil(len(eval_dates) * 0.20)
    formal_dates = eval_dates[obs_count:]
    formal_set = set(formal_dates)
    obs_set = set(eval_dates[:obs_count])

    pre_start = cfg.history_origin - timedelta(days=_PRELOAD_LOOKBACK_DAYS)
    preload_codes = list({*codes, bench})
    sctx = _preload_market_range(preload_codes, pre_start, cfg.eval_end)
    div_index = _preload_dividend_events(codes, pre_start, cfg.eval_end)

    listing, industry = _load_listing_and_industry(codes)
    uni_ctx = UniverseContext.load(codes, UniverseRules())
    exec_rules = ExecutionRules()

    # 采样日集合(确定性,只由 sampling 规则决定)
    sample_dates: dict[tuple[str, str], set[date]] = {}
    for p in profiles.values():
        for comp, spec in p.history_specs.items():
            key = (p.raw_metric_id, comp, spec.sampling)
            if key not in sample_dates:
                sample_dates[key] = compute_sample_dates(dates_all, spec.sampling)

    history = HistoryState()
    scorers = {pid: FactorScorer(profiles[pid]) for pid in alpha_profile_ids}
    aggregator = AlphaAggregator(alpha)

    acct = VirtualAccount(cfg.initial_cash)
    equity_curve: list[dict] = []
    trades: list[dict] = []
    pending_orders: dict[str, float] = {}      # code -> target_weight(昨日信号,今日执行)
    last_target: dict[str, float] = {}
    rebalancer = Rebalancer(portfolio.policy)  # 换手抑制(偏离阈值+冷却+最小持仓;默认0=关)
    last_close: dict[str, float] = {}          # 停牌日估值兜底(沿用上一交易日 close)
    prev_eval_date: date | None = None
    first_trade_date: date | None = None
    last_trade_date: date | None = None
    credit_div = cfg.credit_dividends
    if credit_div:
        cash_dividends = _preload_cash_dividends(codes, pre_start, cfg.eval_end)
        stock_dividends = _preload_stock_dividends(codes, pre_start, cfg.eval_end)
    else:
        cash_dividends = {}
        stock_dividends = {}

    # raw_value 缺失统计(按观察/formal 期分桶;预热期不计入诊断)
    raw_missing: dict[str, dict[str, int]] = {
        m: {"obs": 0, "formal": 0} for m in raw_computers}
    raw_total: dict[str, dict[str, int]] = {
        m: {"obs": 0, "formal": 0} for m in raw_computers}

    with _backtest_series_ctx(sctx, div_index):
        for t in dates_all:
            if equity_curve and len(equity_curve) % 50 == 0:
                print(f"  v2 progress: {len(equity_curve)}/{len(dates_all)} days  as_of={t}",
                      flush=True)
            close_q, open_q, day_bars = _get_day_market(
                preload_codes, t, sctx, valuation_basis=cfg.valuation_basis)

            # ---- 1. 执行 t-1 订单(T+1 开盘价;raw 判涨跌停;先卖后买 + 现金约束)----
            if pending_orders:
                open_prices = dict(open_q)
                sells, buys = [], []
                for code in sorted(pending_orders):
                    tw = pending_orders[code]
                    if code not in open_prices and code in close_q:
                        open_prices[code] = close_q[code]
                    px, source = _get_trade_price(code, open_prices, close_q)
                    if px <= 0:
                        continue        # 停牌:信号丢弃(月调下影响小)
                    cur_w = acct.weight(code, open_prices)
                    act = _classify(cur_w, tw)
                    if act == "hold":
                        continue
                    bar = day_bars.get(code, {})
                    side = "sell" if act in ("sell", "reduce") else "buy"
                    fill = check_fill(
                        side, px,
                        pct_chg=bar.get("pct_chg"),
                        open_=bar.get("open_raw") or bar.get("open"),
                        high=bar.get("high_raw") or bar.get("high"),
                        low=bar.get("low_raw") or bar.get("low"),
                        close=bar.get("close_raw") or bar.get("close"),
                        board=uni_ctx.board(code),
                        is_st=bool(bar.get("is_st")),
                        trade_status=int(bar.get("trade_status", 1)),
                        pre_close=infer_pre_close(
                            bar.get("close_raw") or bar.get("close"), bar.get("pct_chg")),
                        rules=exec_rules,
                    )
                    if not fill.ok:
                        continue
                    if side == "sell":
                        sells.append((code, tw, fill.price, source))
                    else:
                        buys.append((code, tw, fill.price, source))
                # 先卖(释放现金)
                for code, tw, px, source in sells:
                    shares_before = acct.positions[code].shares if code in acct.positions else 0
                    tr = acct.apply_action(code, "reduce", tw, px, open_prices, as_of=t)
                    if tr:
                        tr.update(date=t.isoformat(), status="filled",
                                  price_source=source, reason="v2_open_exec")
                        trades.append(tr)
                        first_trade_date = first_trade_date or t
                        last_trade_date = t
                        if (acct.positions[code].shares if code in acct.positions else 0) == 0:
                            rebalancer.record_close(code)
                # 买单等比缩放到现金
                scaled, _safety, _constrained = scale_buys_to_cash(
                    acct, [(c, tw, px) for c, tw, px, _ in buys], open_prices,
                    commission_rate=COMMISSION_RATE, transfer_fee_rate=TRANSFER_FEE_RATE,
                    min_commission=MIN_COMMISSION)
                scaled_by = {c: (stw, spx) for c, stw, spx in scaled}
                for code, tw, px, source in buys:
                    stw, spx = scaled_by.get(code, (tw, px))
                    shares_before = acct.positions[code].shares if code in acct.positions else 0
                    tr = acct.apply_action(code, "buy", stw, spx, open_prices, as_of=t)
                    if tr:
                        tr.update(date=t.isoformat(), status="filled",
                                  price_source=source, reason="v2_open_exec")
                        trades.append(tr)
                        first_trade_date = first_trade_date or t
                        last_trade_date = t
                        rebalancer.record_buy(code, t, was_new=shares_before == 0)
                pending_orders = {}

            # 分红结算(qfq 下 credit_dividends=False → 跳过)
            if credit_div:
                trades.extend(settle_dividends(
                    acct, t, cash_dividends, stock_dividends, credit_div))

            # ---- 2. universe + 3. raw ----
            day_flags: dict[str, DayFlags] = {}
            for c in codes:
                bar = day_bars.get(c)
                if bar:
                    day_flags[c] = DayFlags(
                        is_st=bool(bar.get("is_st")),
                        trade_status=int(bar.get("trade_status", 1)),
                        has_row=True, amount=bar.get("amount"))
                else:
                    day_flags[c] = DayFlags(has_row=False)
            eligible = {c for c in uni_ctx.eligible_on(t, day_flags) if c in close_q}

            # raw 分期属:obs/formal 才计入 missing_rate 诊断,预热期(None)不计
            period = "obs" if t in obs_set else ("formal" if t in formal_set else None)
            raw_by_metric: dict[str, dict[str, RawFactorObservation]] = {}
            for metric, computer in raw_computers.items():
                m = {}
                for c in eligible:
                    obs = computer(c, t)
                    m[c] = obs
                    if period is not None:
                        raw_total[metric][period] += 1
                        if not obs.valid:
                            raw_missing[metric][period] += 1
                raw_by_metric[metric] = m

            # ---- 4. 评分(仅 eval 期;读 cutoff<t 的历史)----
            is_eval = t in obs_set or t in formal_set
            in_obs = t in obs_set
            strategy_scores = {}
            if is_eval:
                cutoff = history.cutoff          # < t(上一交易日 update 后的值)
                for sc in scorers.values():
                    sc.new_day()
                for c in eligible:
                    fs = {}
                    for pid in alpha_profile_ids:
                        metric = pid_to_metric[pid]
                        fs[pid] = scorers[pid].score(
                            raw_by_metric[metric][c], history,
                            industry.get(c), cfg.market_scope, cutoff)
                    strategy_scores[c] = aggregator.aggregate(
                        c, t, fs, reference_cutoff=cutoff,
                        universe_status="in_universe", observation=in_obs)

                # ---- 组合(formal + rebalance 日)----
                if not in_obs and portfolio.is_rebalance_day(t, prev_eval_date):
                    ctx = DayContext(
                        price={c: (day_bars.get(c, {}).get("close_raw")
                                   or day_bars.get(c, {}).get("close") or 0.0) for c in eligible},
                        amount_20d={c: _amount_20d(sctx, c, t) for c in eligible},
                        listing_date=listing, is_st={c: day_flags[c].is_st for c in eligible},
                    )
                    target = portfolio.select_target(strategy_scores, ctx, t)
                    target = risk.apply(target, acct, close_q, t)
                    last_target = target
                # 生成 t+1 待执行订单(rebalancer 按偏离阈值+冷却+最小持仓[软锁]筛选)
                if not in_obs:
                    held = {c for c, p in acct.positions.items() if p.shares > 0}
                    cur_w = {c: acct.weight(c, close_q) for c in held | set(last_target)}
                    pnl_pct = {
                        c: close_q[c] / acct.positions[c].avg_cost - 1.0
                        for c in held
                        if acct.positions[c].avg_cost > 0 and close_q.get(c, 0.0) > 0
                    }
                    pending_orders = rebalancer.decide(last_target, cur_w, held, t, pnl_pct)
                prev_eval_date = t

            # ---- 5. 日末:评分完成后追加 t 日观测到历史 ----
            sample_flags: dict[str, dict[str, bool]] = {}
            metric_values: dict[str, dict[str, float | None]] = {}
            for p in profiles.values():
                m = p.raw_metric_id
                if m not in metric_values:
                    metric_values[m] = {
                    c: obs.raw_value for c, obs in raw_by_metric.get(m, {}).items()}
                    flags = {}
                    for comp, spec in p.history_specs.items():
                        flags[_COMP_SHORT[comp]] = t in sample_dates[(m, comp, spec.sampling)]
                    sample_flags[m] = flags
            history.update(t, metric_values, industry, cfg.market_scope, sample_flags)

            # ---- equity 记录(qfq 估值;停牌日沿用 last_close,避免持仓估值跳 0)----
            last_close.update({c: v for c, v in close_q.items() if v > 0})
            eq = acct.equity({**last_close, **close_q})
            equity_curve.append({"date": t, "equity": eq, "cash": acct.cash,
                                 "n_positions": sum(1 for p in acct.positions.values() if p.shares > 0),
                                 "in_obs": in_obs, "is_formal": t in formal_set})

    # ---- 绩效(formal 段;§15:净值从 formal 起归一)----
    formal_equity = [p for p in equity_curve if p["is_formal"]]
    formal_bench = _build_benchmark_curve(sctx, bench, [p["date"] for p in formal_equity])

    days = len(formal_equity)
    metrics = _metrics(formal_equity, formal_bench, cfg.initial_cash, days) if formal_equity else {}

    obs_summary = _raw_summary(
        eval_dates[:obs_count],
        {m: raw_missing[m]["obs"] for m in raw_missing},
        {m: raw_total[m]["obs"] for m in raw_total}, "observation")
    formal_summary = _raw_summary(
        formal_dates,
        {m: raw_missing[m]["formal"] for m in raw_missing},
        {m: raw_total[m]["formal"] for m in raw_total}, "formal")

    manifest = cfg.manifest(
        observation_count=obs_count,
        formal_start=formal_dates[0].isoformat() if formal_dates else None,
        first_trade_date=first_trade_date.isoformat() if first_trade_date else None,
        last_trade_date=last_trade_date.isoformat() if last_trade_date else None,
        n_trades=len(trades),
    )
    manifest["run_id"] = fingerprint(manifest, prefix="v2.run")

    return V2Result(
        metrics=metrics, equity_curve=equity_curve,
        formal_equity_curve=formal_equity, benchmark=formal_bench, trades=trades,
        manifest=manifest, history_checkpoint=history.to_checkpoint(),
        observation_summary=obs_summary, formal_summary=formal_summary,
        first_trade_date=first_trade_date, last_trade_date=last_trade_date,
    )


def _build_benchmark_curve(sctx, bench_code: str, formal_dates: list[date]) -> list[dict]:
    """formal 段基准净值(归一为 1)。从列式预载取 benchmark qfq close。"""
    if sctx is None or not formal_dates:
        return []
    cols = sctx.series.get(bench_code)
    if cols is None:
        return []
    closes = cols.get("c") or cols.get("c_raw") or cols.get("close")
    if closes is None:
        return []
    out: list[dict] = []
    base = None
    for d in formal_dates:
        di = sctx.date_idx.get(d)
        if di is None:
            continue
        v = closes[di]
        if math.isnan(v):
            continue
        if base is None:
            base = v
        out.append({"date": d, "equity": v / base})
    return out


def _raw_summary(period_dates: list[date],
                 raw_missing: dict[str, int], raw_total: dict[str, int],
                 label: str) -> dict:
    return {
        "label": label,
        "n_days": len(period_dates),
        "raw_total": dict(raw_total),
        "missing_count": dict(raw_missing),
        "missing_rate": {m: (round(raw_missing[m] / raw_total[m], 4) if raw_total[m] else None)
                         for m in raw_missing},
    }
