"""行业级轮动回测探测(Phase 1)。

在 31 个申万一级行业指数上验证「排除恐/贪/热 top3 行业 → 剩余里选高恐慌/低贪婪/周布林下轨
→ 按离下轨距离定仓 → 接近上轨分批卖 → 20% 止损」的行业择时有没有 edge。

**独立模拟器**,不进四层架构(算子/rebalancer/engine.run_backtest);复用 engine/action/
composite/weekly_bollinger 的纯函数保证信号与执行口径与正式管线一致,但绕开引擎「quote 路由
只认 QuoteSnapshot」的硬限制(指数行情在 index_quote_daily,引擎读不到)。

⚠ 保真度简化(仅判 edge 用,Phase 2 个股化时修正):
  - 当日收盘成交(非引擎 T+1 开盘):每回合约偏多 1 根滑点。
  - 指数也计印花税(沿用引擎费率):保守高估成本。
  - 31 个 SW 代码是 2021 现行分类,2021 后被合并/剔除的行业缺席 → 存活偏差,偏乐观。

用法:python3 -m stockfu.backtest.probes.sector_rotation [--panic-direction both]
"""
from __future__ import annotations

import argparse
import bisect
import json
from dataclasses import dataclass
from datetime import date, timedelta

from sqlmodel import select

from stockfu.db import init_db, session_scope
from stockfu.models import IndexQuoteDaily, QuoteSnapshot
from stockfu.services import composite as C
from stockfu.services import factors as F
from stockfu.ai.operators.factors.weekly_bollinger import (
    _calc_bollinger, _weekly_series_from_rows)
from stockfu.ai.action import PositionManager, resolve_action
from stockfu.backtest.engine import (
    BENCHMARK, COMMISSION_RATE, INITIAL_CASH, MIN_COMMISSION, STAMP_DUTY_RATE,
    TRANSFER_FEE_RATE, Position, VirtualAccount, _apply_gross_cap,
    _benchmark_curve, _metrics, _trade_calendar_days)
from stockfu.scheduler.jobs import INDUSTRY_ETFS, SW_INDUSTRIES

SW_CODES = [f"sw{c}" for c in SW_INDUSTRIES]   # 31 个申万行业指数 asset_code
ETF_CODES = list(INDUSTRY_ETFS)                 # 行业 ETF(可交易标的),代码→行业见 INDUSTRY_ETFS
MID = F.WINDOW_MID_DAYS                          # ~5 年情绪分位窗口,对齐 composite


# ============================================================
# 情绪(对齐 composite.compute_for 的 K 线分桶口径)
# ============================================================

def compute_sentiment(closes: list[float], amounts: list[float]) -> dict | None:
    """行业指数 K 线 → {fear, greed, heat}(各 0-100)或 None(样本不足,该行业当日排除)。

    口径同 composite.compute_for:波动率分位→fear;5日涨幅分位→(100-chg)fear/(chg)greed;
    成交额分位→greed+heat。样本<30 返回 None。
    """
    if len(closes) < 30:
        return None
    vols = C._rolling_vol(closes, 20)
    chgs = C._rolling_chg(closes, 5)
    fp, gp, hp = [], [], []
    if vols:
        p = F.percentile(vols, vols[-1])[0]
        if p is not None:
            fp.append(p)                       # 高波 → fear
    if chgs:
        p = F.percentile(chgs, chgs[-1])[0]
        if p is not None:
            fp.append(100 - p)                 # 跌 → fear
            gp.append(p)                       # 涨 → greed
    if len(amounts) >= 10:
        p = F.percentile(amounts, amounts[-1])[0]
        if p is not None:
            gp.append(p)
            hp.append(p)                       # 放量 → greed + heat
    if not (fp or gp or hp):
        return None
    return {
        "fear": round(sum(fp) / len(fp), 2) if fp else None,
        "greed": round(sum(gp) / len(gp), 2) if gp else None,
        "heat": round(sum(hp) / len(hp), 2) if hp else None,
    }


# ============================================================
# 轮动策略(纯函数,可单测)
# ============================================================

def selection_score(v: dict, panic_direction: str) -> float:
    """候选优先级(越高越先入选):贴下轨(pct_b 低)+ 不贪(greed 低)+ (高恐版:fear 高 / 低恐版:fear 低)。"""
    fear, greed, pct_b = v.get("fear"), v.get("greed"), v.get("pct_b")
    if fear is None or greed is None or pct_b is None:
        return -1e9
    fear_term = fear if panic_direction == "high" else (100 - fear)
    return fear_term - greed - pct_b * 40.0


def rotation_policy(cross: dict, *, panic_direction: str, exclude_top_n: int = 3,
                    boll_buy_max: float = 0.3, fear_high: float = 60.0,
                    greed_low: float = 40.0, max_positions: int = 8,
                    max_w_per_industry: float = 0.20) -> dict[str, float]:
    """轮动 → {code: 目标权重}(仅入场目标;出场由日循环每日处理)。

    1) 排除 fear/greed/heat 各 top-N 并集  2) 筛选(方向×fear + greed 低 + %b 下轨区)
    3) selection_score 排序取前 max_positions(**不凑数**,合格数不足则只持合格的)
    4) 按离下轨距离定仓(越贴下轨越大)。每行业最多 1 只 = 1 code,天然满足(是上限不是定额)。
    """
    # ① 排除并集
    excl: set[str] = set()
    for metric in ("fear", "greed", "heat"):
        ranked = sorted(((c, cross[c][metric]) for c in cross
                         if cross[c].get(metric) is not None),
                        key=lambda x: x[1], reverse=True)
        excl.update(c for c, _ in ranked[:exclude_top_n])
    eligible = {c: v for c, v in cross.items() if c not in excl}

    # ② 筛选
    fear_cut = fear_high if panic_direction == "high" else (100 - fear_high)
    cands: dict[str, dict] = {}
    for c, v in eligible.items():
        fear, greed, pct_b = v.get("fear"), v.get("greed"), v.get("pct_b")
        if fear is None or greed is None or pct_b is None:
            continue
        if panic_direction == "high" and fear < fear_cut:
            continue
        if panic_direction == "low" and fear > fear_cut:
            continue
        if greed > greed_low:
            continue
        if pct_b > boll_buy_max:
            continue
        cands[c] = v

    # ③ 排序 + 持仓上限(不凑数)
    chosen = sorted(cands, key=lambda c: selection_score(cands[c], panic_direction),
                    reverse=True)[:max_positions]

    # ④ 定仓:离下轨越近越大
    targets: dict[str, float] = {}
    for c in chosen:
        pct_b = cands[c]["pct_b"]
        closeness = (max(0.0, min(1.0, (boll_buy_max - pct_b) / boll_buy_max))
                     if boll_buy_max > 0 else 0.0)
        targets[c] = round(max_w_per_industry * closeness, 4)
    return targets


def rotation_policy_stocks(cross: dict, industry_of: dict, *, panic_direction: str,
                           exclude_top_n: int = 3, boll_buy_max: float = 0.3,
                           fear_high: float = 60.0, greed_low: float = 40.0,
                           max_positions: int = 8, max_w_per_industry: float = 0.20
                           ) -> dict[str, float]:
    """个股版轮动:行业级排除/筛选(用行业情绪)+ 每行业选 1 只股票(用个股 %b)。

    cross[code] = {fear,greed,heat(行业级,继承), pct_b(个股), pct_b_ind(行业), close}。
    1) 行业级 top-N 排除(并集)2) 行业级筛选(方向×fear + greed 低 + 行业 %b≤下轨)3) 行业按 score 排序取前 max_positions
    4) 每选中行业挑 1 只股票(contrarian:个股 %b 最低=最超跌;calm:最高)5) 按个股 %b 定仓。
    """
    # 行业情绪(从任一成员继承,同行业相同)
    ind_sent: dict[str, dict] = {}
    for code, v in cross.items():
        ind = industry_of.get(code)
        if ind and ind not in ind_sent and v.get("fear") is not None:
            ind_sent[ind] = v
    # ① 排除行业 top-N 并集
    excl_ind: set[str] = set()
    for metric in ("fear", "greed", "heat"):
        ranked = sorted(((i, ind_sent[i][metric]) for i in ind_sent
                         if ind_sent[i].get(metric) is not None),
                        key=lambda x: x[1], reverse=True)
        excl_ind.update(i for i, _ in ranked[:exclude_top_n])
    # ② 行业筛选
    fear_cut = fear_high if panic_direction == "high" else (100 - fear_high)
    sel_score: dict[str, float] = {}
    for i, v in ind_sent.items():
        if i in excl_ind:
            continue
        f, g, pbi = v.get("fear"), v.get("greed"), v.get("pct_b_ind")
        if f is None or g is None or pbi is None:
            continue
        if panic_direction == "high" and f < fear_cut:
            continue
        if panic_direction == "low" and f > fear_cut:
            continue
        if g > greed_low:
            continue
        if pbi > boll_buy_max:
            continue
        sel_score[i] = selection_score(
            {"fear": f, "greed": g, "pct_b": pbi}, panic_direction)
    # ③ 行业排序 + 上限
    chosen_ind = sorted(sel_score, key=lambda i: sel_score[i], reverse=True)[:max_positions]
    # ④ 每行业挑 1 只 + 定仓
    targets: dict[str, float] = {}
    for ind in chosen_ind:
        members = [c for c in cross if industry_of.get(c) == ind
                   and cross[c].get("pct_b") is not None]
        if not members:
            continue
        pick = (min(members, key=lambda c: cross[c]["pct_b"]) if panic_direction == "high"
                else max(members, key=lambda c: cross[c]["pct_b"]))
        pct_b = cross[pick]["pct_b"]
        closeness = (max(0.0, min(1.0, (boll_buy_max - pct_b) / boll_buy_max))
                     if boll_buy_max > 0 else 0.0)
        targets[pick] = round(max_w_per_industry * closeness, 4)
    return targets


def _ladder_weight(pct_b: float, cur_w: float,
                   rungs=((0.70, 1.0), (0.85, 0.5), (1.00, 0.0))) -> float:
    """接近上轨阶梯减仓:pct_b 越高保留越少(0.70 起触发,0.85 留半,1.0 清)。返回目标权重(≤cur_w)。"""
    keep = 1.0
    for thresh, frac in sorted(rungs):
        if pct_b >= thresh:
            keep = min(keep, frac)
    return cur_w * keep


# ============================================================
# 名义账户(分数仓位,去整百股)
# ============================================================

class NotionalAccount(VirtualAccount):
    """行业指数按权重交易(分数股,无整百股)。复用 VirtualAccount 的 equity/weight/费率,
    仅重写 apply_action 去掉 int(.../100)*100 的整百股逻辑。"""

    def apply_action(self, code: str, action: str, target_weight: float,
                     price: float, prices: dict[str, float]) -> dict | None:
        if price <= 0 or action == "hold":
            return None
        total = self.equity(prices)
        if total <= 0:
            return None
        target_value = target_weight * total
        pos = self.positions.setdefault(code, Position())
        current_value = pos.shares * price
        delta = target_value - current_value
        if abs(delta) < total * 0.001:
            return None
        if delta > 0:                                   # 买入(受现金约束)
            shares = min(delta, self.cash) / price      # 分数股
            if shares <= 0:
                return None
            cost = shares * price
            fee = max(cost * COMMISSION_RATE, MIN_COMMISSION) + cost * TRANSFER_FEE_RATE
            pos.avg_cost = (pos.avg_cost * pos.shares + cost) / (pos.shares + shares)
            pos.shares += shares
            self.cash -= (cost + fee)
            self.fee_paid += fee
            return {"kind": action, "code": code, "shares": round(shares, 4),
                    "price": price, "fee": round(fee, 2), "pnl": None}
        else:                                           # 卖出
            shares = min((-delta) / price, pos.shares)
            if shares <= 0:
                return None
            proceeds = shares * price
            fee = (max(proceeds * COMMISSION_RATE, MIN_COMMISSION)
                   + proceeds * (STAMP_DUTY_RATE + TRANSFER_FEE_RATE))
            realized = (price - pos.avg_cost) * shares - fee
            pos.shares -= shares
            self.cash += (proceeds - fee)
            self.fee_paid += fee
            if pos.shares <= 1e-9:
                pos.shares = 0
                pos.avg_cost = 0.0
            return {"kind": action, "code": code, "shares": -round(shares, 4),
                    "price": price, "fee": round(fee, 2), "pnl": round(realized, 2)}


# ============================================================
# 数据预载 + 横截面
# ============================================================

def _quote_model(code: str):
    """按代码路由行情表:sw/sh/sz→IndexQuoteDaily;1/5 开头(ETF)→EtfQuoteDaily;其余(0/3/6 股票)→QuoteSnapshot。"""
    from stockfu.models import EtfQuoteDaily
    if code.startswith(("sw", "sh", "sz")):
        return IndexQuoteDaily
    if code[:1] in ("1", "5"):
        return EtfQuoteDaily
    return QuoteSnapshot


def _preload(codes: list[str], end: date) -> dict[str, dict]:
    """预载给定 codes 截至 end 的完整行情(升序),按代码路由到指数/ETF 表,供日循环 bisect 切片。"""
    pre: dict[str, dict] = {}
    with session_scope() as s:
        for code in codes:
            model = _quote_model(code)
            rows = s.exec(select(model).where(
                model.asset_code == code,
                model.quote_date <= end,
            ).order_by(model.quote_date)).all()
            if rows:
                pre[code] = {"dates": [r.quote_date for r in rows], "rows": rows}
    return pre


def load_stock_universe(top_k: int = 10) -> tuple[dict[str, str], list[str]]:
    """个股宇宙:每个申万行业取 index_component_sw 的 top_k×3 权重成分股,筛掉不在 quote_snapshot 池里的。

    返回 (industry_of: {stock_code: 行业名}, stock_codes: list)。网络 ~18 次(index_component_sw)。
    只取"有 ETF 代表"的 18 个行业(INDUSTRY_ETFS.values()),与 ETF 版行业口径一致。
    """
    sw_code_of = {name: code for code, name in SW_INDUSTRIES.items()}
    wanted = list(INDUSTRY_ETFS.values())
    industry_of: dict[str, str] = {}
    with session_scope() as s:
        pool = set(s.exec(select(QuoteSnapshot.asset_code).distinct()).all())
    from stockfu.data.base import direct_connection
    with direct_connection():
        try:
            import akshare as ak
        except Exception:
            return {}, []
        for name in wanted:
            swc = sw_code_of.get(name)
            if not swc:
                continue
            try:
                df = ak.index_component_sw(symbol=swc)
            except Exception:
                continue
            if df is None or df.empty:
                continue
            # 取 top_k×3 再筛池子(命中率~16%),保证筛后≈top_k 个有行情的权重股
            for _, r in df.sort_values("最新权重", ascending=False).head(top_k * 3).iterrows():
                code = str(r["证券代码"]).zfill(6)
                if code in pool and code not in industry_of:
                    industry_of[code] = name
                    if len([c for c in industry_of if industry_of[c] == name]) >= top_k:
                        break
    return industry_of, sorted(industry_of)


def _cross_one(pre_code: dict, as_of: date, boll_window: int, boll_k: float,
               want_sentiment: bool, max_bars: int = 1500) -> dict | None:
    """单行业截至 as_of 的横截面(周布林 %b [+ 情绪])。样本不足返 None。点在时点(<=as_of)。"""
    dates, rows = pre_code["dates"], pre_code["rows"]
    i = bisect.bisect_right(dates, as_of)
    if i < 30:
        return None
    window = rows[max(0, i - max_bars):i]
    closes = [r.close for r in window if r.close is not None]
    if len(closes) < 30:
        return None
    _, weekly_closes = _weekly_series_from_rows(window)
    if len(weekly_closes) < boll_window:
        return None
    _, up, lo, _ = _calc_bollinger(weekly_closes, boll_window, boll_k)
    if up is None or lo is None or up <= lo:
        return None
    out: dict = {"close": closes[-1], "upper": up, "lower": lo,
                 "pct_b": round((closes[-1] - lo) / (up - lo), 4)}
    if want_sentiment:
        amounts = [r.amount for r in window if r.amount is not None]
        sent = compute_sentiment(closes, amounts)
        if sent:
            out.update(sent)
    return out


# ============================================================
# 主回测
# ============================================================

def run_probe(start: date, end: date, *, panic_direction: str = "high",
              rebalance_freq: str = "weekly", max_gross: float = 0.95,
              max_positions: int = 8, stop_loss_pct: float = 0.20,
              boll_window: int = 20, boll_k: float = 2.0, boll_buy_max: float = 0.3,
              fear_high: float = 60.0, greed_low: float = 40.0,
              max_w_per_industry: float = 0.20, buy_cool_down_days: int = 0,
              max_target_step: float = 1.0, lock_days: int = 0,
              codes: list[str] | None = None,
              initial_cash: float = INITIAL_CASH, pre: dict | None = None) -> dict:
    uni = codes or SW_CODES
    days = _trade_calendar_days(start, end)
    if pre is None:
        pre = _preload(uni, end)
    acct = NotionalAccount(initial_cash)
    pm = PositionManager(buy_cool_down_days=buy_cool_down_days,
                         max_target_step=max_target_step, min_trade_weight=0.0)
    equity_curve: list[dict] = []
    holdings_curve: list[dict] = []
    trades: list[dict] = []
    prev_week, prev_month, last_close = None, None, {}
    entry_idx: dict[str, int] = {}              # code → 建仓日在 days 中的下标(锁用)

    for idx, as_of in enumerate(days):
        held = {c for c, p in acct.positions.items() if p.shares > 0}
        iso = as_of.isocalendar()[:2]
        if rebalance_freq == "daily":
            is_reb = True
        elif rebalance_freq == "monthly":
            is_reb = as_of.month != prev_month
        else:                                   # weekly(默认)
            is_reb = iso != prev_week
        prev_week, prev_month = iso, as_of.month

        # ① 横截面:调仓日全标的(选股需要);非调仓日仅 held(出场只需 %b+close)
        cross: dict[str, dict] = {}
        for code in (uni if is_reb else held):
            pcd = pre.get(code)
            if not pcd:
                continue
            v = _cross_one(pcd, as_of, boll_window, boll_k, want_sentiment=is_reb)
            if v:
                cross[code] = v
                last_close[code] = v["close"]
        prices = {c: v["close"] for c, v in cross.items()}
        for c in held:                          # held-but-unquoted 兜价
            if c not in prices and c in last_close:
                prices[c] = last_close[c]

        # ② 目标仓位:调仓日=轮动选股(held 未入选→0 轮出);非调仓日=持有当前
        if is_reb:
            entries = rotation_policy(
                cross, panic_direction=panic_direction, exclude_top_n=3,
                boll_buy_max=boll_buy_max, fear_high=fear_high, greed_low=greed_low,
                max_positions=max_positions, max_w_per_industry=max_w_per_industry)
            targets = {c: entries.get(c, 0.0) for c in (set(cross) | held)}
        else:
            targets = {c: acct.weight(c, prices) for c in held}

        # ③ 出场(每日,风险优先):止损(永不锁)→ 建仓锁定(lock_days 内只持)→ 接近上轨阶梯减仓
        for code in held:
            pos = acct.positions[code]
            info = cross.get(code)
            cw = acct.weight(code, prices)
            px = info["close"] if info else last_close.get(code)
            if px and pos.avg_cost > 0 and px <= pos.avg_cost * (1 - stop_loss_pct):
                targets[code] = 0.0            # 止损:风险优先
                continue
            if lock_days > 0:
                ei = entry_idx.get(code)
                if ei is not None and (idx - ei) < lock_days:
                    targets[code] = cw          # 锁定:不轮出/不减仓(降换手)
                    continue
            if info and info["pct_b"] >= 0.70:
                targets[code] = min(targets.get(code, cw), _ladder_weight(info["pct_b"], cw))

        # ④ 总仓阀(等比裁 Σ正值权重 ≤ max_gross)
        targets = _apply_gross_cap(targets, max_gross)

        # ⑤ 边沿触发 + 执行(as_of 收盘价成交)
        for code, tw in sorted(targets.items()):
            if code not in held and tw <= 0:
                continue                        # 未持仓且目标 0,no-op
            px = prices.get(code)
            if not px:
                continue
            cw = acct.weight(code, prices)
            act, eff_tw, _ = pm.should_act(code, tw, cw, as_of, days)
            action = resolve_action(cw, eff_tw) if act else "hold"
            rec = acct.apply_action(code, action, eff_tw, px, prices)
            if rec:
                trades.append({**rec, "date": as_of.isoformat()})
                if rec["kind"] == "buy":       # 新建仓 → 记建仓日
                    entry_idx[code] = idx
                elif rec["kind"] == "sell":    # 清仓 → 解锁
                    entry_idx.pop(code, None)

        # ⑥ 记录
        equity_curve.append({"date": as_of.isoformat(),
                             "equity": round(acct.equity(prices), 2)})
        holdings_curve.append({"date": as_of.isoformat(),
                               "positions": [{"code": c, "weight": round(acct.weight(c, prices) * 100, 2)}
                                             for c, p in acct.positions.items() if p.shares > 0]})

    # ⑦ 指标 + 基准 + 换手
    benchmark, bench_window = _benchmark_curve(BENCHMARK, days)
    metrics = _metrics(equity_curve, benchmark, initial_cash, len(days), bench_window)
    _tov, _n = 0.0, []
    for i in range(1, len(holdings_curve)):
        a = {p["code"] for p in holdings_curve[i - 1]["positions"]}
        b = {p["code"] for p in holdings_curve[i]["positions"]}
        _tov += len(a ^ b) / 2.0
        _n.append(len(b))
    metrics["turnover_count"] = round(_tov, 1)
    _avg_n = sum(_n) / len(_n) if _n else 0.0
    _years = len(days) / 252.0
    metrics["annual_turnover"] = (round((_tov / _years) / _avg_n, 2)
                                  if _years > 0 and _avg_n > 0 else None)
    metrics["avg_positions"] = round(_avg_n, 2)
    metrics["trade_count"] = len(trades)
    metrics["final_equity"] = equity_curve[-1]["equity"] if equity_curve else None
    return {"equity_curve": equity_curve, "holdings_curve": holdings_curve,
            "trades": trades, "metrics": metrics}


def run_stock_probe(start: date, end: date, *, panic_direction: str = "high",
                    rebalance_freq: str = "weekly", max_gross: float = 0.95,
                    max_positions: int = 8, stop_loss_pct: float = 0.20,
                    boll_window: int = 20, boll_k: float = 2.0, boll_buy_max: float = 0.3,
                    fear_high: float = 60.0, greed_low: float = 40.0,
                    max_w_per_industry: float = 0.20, buy_cool_down_days: int = 0,
                    max_target_step: float = 1.0, lock_days: int = 0, stock_top_k: int = 10,
                    initial_cash: float = INITIAL_CASH) -> dict:
    """个股版探测:行业情绪(ETF 派生)驱动行业排除/筛选,每个选中行业挑 1 只个股(个股周布林 %b)。

    两层横截面:① ETF 行业情绪/行业%b(同 ETF 版)② 个股 %b(quote_snapshot)。
    rotation_policy_stocks 做行业级排除/筛选 + 每行业 1 只。出场/执行同 run_probe。
    """
    days = _trade_calendar_days(start, end)
    pre_etf = _preload(ETF_CODES, end)
    industry_of, stock_codes = load_stock_universe(top_k=stock_top_k)
    pre_stk = _preload(stock_codes, end) if stock_codes else {}
    acct = NotionalAccount(initial_cash)
    pm = PositionManager(buy_cool_down_days=buy_cool_down_days,
                         max_target_step=max_target_step, min_trade_weight=0.0)
    equity_curve: list[dict] = []
    holdings_curve: list[dict] = []
    trades: list[dict] = []
    prev_week, prev_month, last_close = None, None, {}
    entry_idx: dict[str, int] = {}

    for idx, as_of in enumerate(days):
        held = {c for c, p in acct.positions.items() if p.shares > 0}
        iso = as_of.isocalendar()[:2]
        if rebalance_freq == "daily":
            is_reb = True
        elif rebalance_freq == "monthly":
            is_reb = as_of.month != prev_month
        else:
            is_reb = iso != prev_week
        prev_week, prev_month = iso, as_of.month

        # ① 行业情绪(ETF)+ 行业 %b
        ind_cross: dict[str, dict] = {}
        for ec in ETF_CODES:
            pcd = pre_etf.get(ec)
            if not pcd:
                continue
            v = _cross_one(pcd, as_of, boll_window, boll_k, want_sentiment=True)
            if v:
                ind_cross[INDUSTRY_ETFS[ec]] = v      # 行业名 → {fear,greed,heat,close,pct_b}

        # ② 个股横截面(调仓日全部;非调仓日仅 held)+ 继承行业情绪
        need_stk = stock_codes if is_reb else [c for c in held if c in pre_stk]
        cross: dict[str, dict] = {}
        for code in need_stk:
            iv = ind_cross.get(industry_of.get(code))
            pcd = pre_stk.get(code)
            if not iv or not pcd:
                continue
            sv = _cross_one(pcd, as_of, boll_window, boll_k, want_sentiment=False)
            if not sv:
                continue
            cross[code] = {"fear": iv.get("fear"), "greed": iv.get("greed"),
                           "heat": iv.get("heat"), "pct_b": sv["pct_b"],
                           "pct_b_ind": iv.get("pct_b"), "close": sv["close"]}
            last_close[code] = sv["close"]
        prices = {c: cross[c]["close"] for c in cross}
        for c in held:
            if c not in prices and c in last_close:
                prices[c] = last_close[c]

        # ③ 目标仓位:调仓日=轮动(行业排除/筛选 + 每行业 1 只);非调仓日=持有
        if is_reb:
            entries = rotation_policy_stocks(
                cross, industry_of, panic_direction=panic_direction, exclude_top_n=3,
                boll_buy_max=boll_buy_max, fear_high=fear_high, greed_low=greed_low,
                max_positions=max_positions, max_w_per_industry=max_w_per_industry)
            targets = {c: entries.get(c, 0.0) for c in (set(cross) | held)}
        else:
            targets = {c: acct.weight(c, prices) for c in held}

        # ④ 出场:止损(永不锁)→ 建仓锁定 → 接近上轨阶梯减仓
        for code in held:
            pos = acct.positions[code]
            info = cross.get(code)
            cw = acct.weight(code, prices)
            px = info["close"] if info else last_close.get(code)
            if px and pos.avg_cost > 0 and px <= pos.avg_cost * (1 - stop_loss_pct):
                targets[code] = 0.0
                continue
            if lock_days > 0:
                ei = entry_idx.get(code)
                if ei is not None and (idx - ei) < lock_days:
                    targets[code] = cw
                    continue
            if info and info["pct_b"] >= 0.70:
                targets[code] = min(targets.get(code, cw), _ladder_weight(info["pct_b"], cw))

        # ⑤ 总仓阀 + 边沿触发 + 执行
        targets = _apply_gross_cap(targets, max_gross)
        for code, tw in sorted(targets.items()):
            if code not in held and tw <= 0:
                continue
            px = prices.get(code)
            if not px:
                continue
            cw = acct.weight(code, prices)
            act, eff_tw, _ = pm.should_act(code, tw, cw, as_of, days)
            action = resolve_action(cw, eff_tw) if act else "hold"
            rec = acct.apply_action(code, action, eff_tw, px, prices)
            if rec:
                trades.append({**rec, "date": as_of.isoformat()})
                if rec["kind"] == "buy":
                    entry_idx[code] = idx
                elif rec["kind"] == "sell":
                    entry_idx.pop(code, None)

        equity_curve.append({"date": as_of.isoformat(), "equity": round(acct.equity(prices), 2)})
        holdings_curve.append({"date": as_of.isoformat(),
                               "positions": [{"code": c, "weight": round(acct.weight(c, prices) * 100, 2)}
                                             for c, p in acct.positions.items() if p.shares > 0]})

    benchmark, bench_window = _benchmark_curve(BENCHMARK, days)
    metrics = _metrics(equity_curve, benchmark, initial_cash, len(days), bench_window=bench_window)
    _tov, _n = 0.0, []
    for i in range(1, len(holdings_curve)):
        a = {p["code"] for p in holdings_curve[i - 1]["positions"]}
        b = {p["code"] for p in holdings_curve[i]["positions"]}
        _tov += len(a ^ b) / 2.0
        _n.append(len(b))
    metrics["turnover_count"] = round(_tov, 1)
    _avg_n = sum(_n) / len(_n) if _n else 0.0
    _years = len(days) / 252.0
    metrics["annual_turnover"] = (round((_tov / _years) / _avg_n, 2)
                                  if _years > 0 and _avg_n > 0 else None)
    metrics["avg_positions"] = round(_avg_n, 2)
    metrics["trade_count"] = len(trades)
    metrics["final_equity"] = equity_curve[-1]["equity"] if equity_curve else None
    metrics["universe_stocks"] = len(stock_codes)
    return {"equity_curve": equity_curve, "holdings_curve": holdings_curve,
            "trades": trades, "metrics": metrics}


# ============================================================
# CLI
# ============================================================

def _print(direction: str, m: dict) -> None:
    print(f"【panic={direction}】 总收益 {m.get('total_return')}% | 年化 {m.get('annualized')}% | "
          f"夏普 {m.get('sharpe')} | 回撤 {m.get('max_drawdown')}% | Calmar {m.get('calmar')} | "
          f"超额 {m.get('excess')}%(基准 {m.get('benchmark_return')}%) | "
          f"换手 {m.get('annual_turnover')}遍 | 均仓 {m.get('avg_positions')}只 | "
          f"交易 {m.get('trade_count')}笔 | 期末 {m.get('final_equity')}")


def main() -> None:
    ap = argparse.ArgumentParser(description="申万行业级轮动回测探测(Phase 1)")
    ap.add_argument("--start", default="2021-01-04")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--panic-direction", choices=["high", "low", "both"], default="both")
    ap.add_argument("--universe", choices=["sw", "etf", "stock", "both", "all"], default="etf",
                    help="标的池:sw=申万行业指数 / etf=行业ETF / stock=个股(每行业top-K) / both=sw+etf / all=三者")
    ap.add_argument("--stock-top-k", type=int, default=10,
                    help="个股模式:每行业取多少只候选权重股(再选1只)")
    ap.add_argument("--rebalance-freq", choices=["weekly", "daily", "monthly"], default="weekly")
    ap.add_argument("--max-gross", type=float, default=0.95)
    ap.add_argument("--max-positions", type=int, default=8)
    ap.add_argument("--lock-days", type=int, default=0,
                    help="建仓锁定交易日:lock_days 内不轮出/不减仓(降换手,止损仍生效)")
    ap.add_argument("--stop-loss", type=float, default=0.20)
    ap.add_argument("--boll-window", type=int, default=20)
    ap.add_argument("--boll-k", type=float, default=2.0)
    ap.add_argument("--boll-buy-max", type=float, default=0.3)
    ap.add_argument("--max-w-per-industry", type=float, default=0.20)
    ap.add_argument("--fear-high", type=float, default=60.0)
    ap.add_argument("--greed-low", type=float, default=40.0)
    ap.add_argument("--save-curve", default=None,
                    help="存 equity_curve + metrics 的 JSON 路径(可选)")
    args = ap.parse_args()

    init_db()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    dirs = ["high", "low"] if args.panic_direction == "both" else [args.panic_direction]
    uni_map = {"sw": SW_CODES, "etf": ETF_CODES}
    if args.universe == "both":
        universes = ["sw", "etf"]
    elif args.universe == "all":
        universes = ["etf", "stock"]
    else:
        universes = [args.universe]

    print("⚠ 保真度简化:当日收盘成交(非 T+1 开盘)、ETF/指数计印花税、存活偏差。仅判 edge。\n")
    results: dict[str, dict] = {}
    for u in universes:
        for d in dirs:
            if u == "stock":
                print(f"── 标的池=stock（每行业 top{args.stock_top_k} 权重股）──")
                r = run_stock_probe(start, end, panic_direction=d,
                                    rebalance_freq=args.rebalance_freq,
                                    max_gross=args.max_gross, max_positions=args.max_positions,
                                    stop_loss_pct=args.stop_loss, boll_window=args.boll_window,
                                    boll_k=args.boll_k, boll_buy_max=args.boll_buy_max,
                                    fear_high=args.fear_high, greed_low=args.greed_low,
                                    max_w_per_industry=args.max_w_per_industry,
                                    lock_days=args.lock_days, stock_top_k=args.stock_top_k)
            else:
                codes = uni_map[u]
                print(f"── 标的池={u}（{len(codes)} 只）──")
                r = run_probe(start, end, panic_direction=d, rebalance_freq=args.rebalance_freq,
                              max_gross=args.max_gross, max_positions=args.max_positions,
                              stop_loss_pct=args.stop_loss, boll_window=args.boll_window,
                              boll_k=args.boll_k, boll_buy_max=args.boll_buy_max,
                              fear_high=args.fear_high, greed_low=args.greed_low,
                              max_w_per_industry=args.max_w_per_industry, lock_days=args.lock_days,
                              codes=codes)
            key = f"{u}:{d}"
            results[key] = r
            _print(key, r["metrics"])
    if args.save_curve:
        with open(args.save_curve, "w") as f:
            json.dump({d: {"equity_curve": results[d]["equity_curve"],
                           "metrics": results[d]["metrics"]} for d in results}, f,
                      ensure_ascii=False)
        print(f"\n曲线已存 {args.save_curve}")


if __name__ == "__main__":
    main()
