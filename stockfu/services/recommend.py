"""空仓重建荐股:复用策略 + 回测 meta → 信号日 as_of 决策 → 次日开盘执行参考。

与回测末日持仓的区别:
  - 持仓图 = 全周期路径依赖
  - 本服务 = 假设当日空仓,按因子分 + continuous 仓位 + catalog rebalancer,
    得到「若今天从零建仓会选谁」—— 更接近选股推荐。

硬约束:
  - 取数严格 <= as_of(防未来函数)
  - 策略 id 必须在 FULL_CYCLE_CATALOG(与全周期验收口径一致)
  - 默认不写 operator_result(避免与回测抢写锁)
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

import yaml
from sqlalchemy import func
from sqlmodel import select

from stockfu.ai.action import compute_target_weight
from stockfu.ai.operators.registry import discover_and_register
from stockfu.ai.operators.runner import compile_strategy
from stockfu.ai.rebalancers.registry import discover_and_register as discover_rebalancers
from stockfu.ai.rebalancers.registry import get_rebalancer
from stockfu.backtest.full_cycle_update import (
    FULL_CYCLE_CATALOG,
    StrategyRunSpec,
    catalog_ids,
    detect_data_end,
)
from stockfu.db import session_scope
from stockfu.models import QuoteSnapshot, Asset
from stockfu.services.valuation import valuation_snapshot

ROOT = Path(__file__).resolve().parents[2]
STRATEGIES_DIR = ROOT / "stockfu" / "ai" / "strategies"
REPORT_DIR = ROOT / "data" / "reports" / "recommend"

EXEC_NOTE = (
    "信号日收盘决策,下一交易日开盘执行;"
    "suggest_limit 为执行参考限价(非保证成交);"
    "fair_price/value_band 为历史 PE/PB 中枢映射,非承诺目标价;"
    "涨停/停牌可能无法买入(与回测 on_unfillable=defer 一致)。"
)

_CATALOG_BY_ID: dict[str, StrategyRunSpec] = {s.strategy_id: s for s in FULL_CYCLE_CATALOG}


# ── 公共小工具 ─────────────────────────────────────────────────────


def available_strategies() -> list[str]:
    return catalog_ids()


def resolve_strategy_specs(strategy_ids: Iterable[str]) -> list[StrategyRunSpec]:
    """严格解析 catalog;未知 id 抛 ValueError(列出可选)。"""
    wanted: list[str] = []
    unknown: list[str] = []
    for raw in strategy_ids:
        sid = (raw or "").strip()
        if not sid:
            continue
        if sid not in _CATALOG_BY_ID:
            unknown.append(sid)
        else:
            wanted.append(sid)
    if unknown:
        raise ValueError(
            f"未知 strategy_id: {unknown}; 可选: {available_strategies()}"
        )
    if not wanted:
        raise ValueError(
            f"--strategies 必填; 可选: {available_strategies()}"
        )
    order = {s.strategy_id: i for i, s in enumerate(FULL_CYCLE_CATALOG)}
    return sorted(
        (_CATALOG_BY_ID[sid] for sid in set(wanted)),
        key=lambda s: order[s.strategy_id],
    )


def next_trade_date(as_of: date) -> tuple[date | None, str]:
    """下一交易日:优先库内 as_of 之后最早 quote_date;否则跳过周末的日历估计。"""
    with session_scope() as s:
        d = s.exec(
            select(func.min(QuoteSnapshot.quote_date)).where(
                QuoteSnapshot.quote_date > as_of
            )
        ).one()
    if d is not None:
        if isinstance(d, str):
            d = date.fromisoformat(d[:10])
        return d, "db_quote"
    # 数据末日:简单跳过周末(不处理法定假日)
    cand = as_of + timedelta(days=1)
    for _ in range(10):
        if cand.weekday() < 5:
            return cand, "calendar_estimate"
        cand += timedelta(days=1)
    return None, "unknown"


def default_as_of() -> date:
    end = detect_data_end()
    return date.fromisoformat(end[:10])


def _names() -> dict[str, str]:
    """名称: stock_basic 表(无 ORM) + asset 兜底。"""
    out: dict[str, str] = {}
    db_path = ROOT / "data" / "stockfu.db"
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.execute("PRAGMA busy_timeout=15000")
        for code, name in conn.execute("SELECT code, name FROM stock_basic"):
            if code:
                out[code] = name or ""
        conn.close()
    except Exception:
        pass
    with session_scope() as s:
        for r in s.exec(select(Asset)).all():
            if r.code:
                out.setdefault(r.code, r.name or "")
    return out


def _returns(codes: list[str], as_of: date) -> dict[str, dict]:
    if not codes:
        return {}
    db_path = ROOT / "data" / "stockfu.db"
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.execute("PRAGMA busy_timeout=15000")
    start = (as_of - timedelta(days=400)).isoformat()
    by: dict[str, list] = defaultdict(list)
    chunk = 400
    for i in range(0, len(codes), chunk):
        part = codes[i : i + chunk]
        ph = ",".join("?" * len(part))
        rows = conn.execute(
            f"""
            SELECT asset_code, quote_date, close, pct_chg FROM quote_snapshot
            WHERE asset_code IN ({ph}) AND quote_date >= ? AND quote_date <= ?
            ORDER BY asset_code, quote_date
            """,
            (*part, start, as_of.isoformat()),
        ).fetchall()
        for code, qd, close, pct in rows:
            by[code].append((qd, close, pct))
    conn.close()

    def pct(a, b):
        if not a:
            return None
        return (b / a - 1) * 100

    out: dict[str, dict] = {}
    for code, series in by.items():
        i = len(series) - 1
        closes = [s[1] for s in series]
        c0 = closes[i]
        d1 = series[i][2]
        if d1 is None and i >= 1:
            d1 = pct(closes[i - 1], c0)

        def lb(n):
            j = max(0, i - n)
            return pct(closes[j], c0)

        out[code] = {"d1": d1, "w1": lb(5), "m1": lb(21), "y1": lb(250), "close": c0}
    return out


def _sentiment_offline(codes: list[str]) -> dict[str, dict]:
    from stockfu.services.composite import compute_for

    sent: dict[str, dict] = {}
    for i, code in enumerate(codes):
        try:
            r = compute_for(code, "stock", code)
            sent[code] = {
                "fear": r.get("fear"), "greed": r.get("greed"), "heat": r.get("heat"),
            }
        except Exception:
            sent[code] = {"fear": None, "greed": None, "heat": None}
        if (i + 1) % 100 == 0:
            print(f"  sentiment {i + 1}/{len(codes)}", flush=True)
    return sent


def resolve_universe(spec: StrategyRunSpec, as_of: date,
                     min_amount_override: float | None = None) -> list[str]:
    """与 full_cycle_update._resolve_codes 对齐,并在 as_of 日做成交额/有价过滤。"""
    from stockfu.backtest.full_cycle_update import _resolve_codes

    base, _rules = _resolve_codes(spec)
    min_amt = min_amount_override
    if min_amt is None:
        min_amt = spec.min_amount

    if not base:
        return []

    db_path = ROOT / "data" / "stockfu.db"
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.execute("PRAGMA busy_timeout=15000")
    ok: set[str] = set()
    chunk = 400
    for i in range(0, len(base), chunk):
        part = base[i : i + chunk]
        ph = ",".join("?" * len(part))
        # ETF 在 etf_quote_daily;个股在 quote_snapshot
        if spec.universe == "etf":
            rows = conn.execute(
                f"""
                SELECT asset_code, amount, close FROM etf_quote_daily
                WHERE quote_date = ? AND asset_code IN ({ph})
                """,
                (as_of.isoformat(), *part),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT asset_code, amount, close FROM quote_snapshot
                WHERE quote_date = ? AND asset_code IN ({ph})
                """,
                (as_of.isoformat(), *part),
            ).fetchall()
        for code, amount, close in rows:
            if close is None or close <= 0:
                continue
            if min_amt is not None and amount is not None and amount < min_amt:
                continue
            ok.add(code)
    # 成交额过滤过狠 → 退回有收盘价集合
    if len(ok) < 20 and min_amt is not None and spec.universe != "etf":
        ok = set()
        for i in range(0, len(base), chunk):
            part = base[i : i + chunk]
            ph = ",".join("?" * len(part))
            rows = conn.execute(
                f"""
                SELECT asset_code FROM quote_snapshot
                WHERE quote_date = ? AND asset_code IN ({ph}) AND close > 0
                """,
                (as_of.isoformat(), *part),
            ).fetchall()
            ok.update(r[0] for r in rows)
    conn.close()
    return sorted(ok)


def _analyze_all(cs, codes: list[str], as_of: date, write_cache: bool = False) -> dict[str, dict]:
    """缓存优先;默认 miss 不算写库。"""
    import stockfu.ai.operator_cache as oc

    _orig_save = oc.save_operator_result
    _orig_batch = oc.save_operator_results_batch
    if not write_cache:
        oc.save_operator_result = lambda *a, **k: None  # type: ignore
        oc.save_operator_results_batch = lambda *a, **k: 0  # type: ignore

    results: dict[str, dict] = {}
    t0 = time.time()
    try:
        try:
            prefill = cs.prefetch_cache(codes, as_of)
        except Exception as e:
            print(f"  prefetch warn: {e}; fallback per-code", flush=True)
            prefill = None
        for i, code in enumerate(codes):
            try:
                results[code] = cs.analyze(code, as_of=as_of, cache_prefill=prefill)
            except Exception as e:
                results[code] = {"error": str(e)}
            if (i + 1) % 100 == 0:
                print(f"  analyze {i + 1}/{len(codes)}  {time.time() - t0:.0f}s", flush=True)
    finally:
        oc.save_operator_result = _orig_save
        oc.save_operator_results_batch = _orig_batch
    print(f"  analyze done {len(results)} in {time.time() - t0:.0f}s", flush=True)
    return results


def _backtest_meta_for(strategy_id: str) -> dict[str, Any] | None:
    """从 list_runs 取该策略最近一次有 metrics 的摘要(优先长窗口)。"""
    from stockfu.backtest.scheduler import list_runs

    cands = [
        r for r in list_runs()
        if r.get("strategy_id") == strategy_id and r.get("metrics")
    ]
    if not cands:
        return None

    def score(r: dict) -> tuple:
        m = r.get("metrics") or {}
        # 优先有 excess 且窗口含 2021 的全周期
        start = r.get("start") or ""
        end = r.get("end") or ""
        long_win = 1 if start.startswith("2021") else 0
        has_excess = 1 if m.get("excess") is not None else 0
        return (long_win, has_excess, end)

    best = max(cands, key=score)
    m = best.get("metrics") or {}
    bw = m.get("benchmark_window") or {}
    window = None
    if bw.get("start") and bw.get("end"):
        window = f"{bw['start']}→{bw['end']}"
    elif best.get("start") and best.get("end"):
        window = f"{best['start']}→{best['end']}"
    return {
        "run_id": best.get("run_id"),
        "total_return_pct": m.get("total_return"),
        "annualized_pct": m.get("annualized"),
        "excess_pct": m.get("excess"),
        "max_drawdown_pct": m.get("max_drawdown"),
        "sharpe": m.get("sharpe"),
        "window": window,
        "start": best.get("start"),
        "end": best.get("end"),
    }


def _exec_prices(ref: float | None, slip_bps: float, band_pct: float) -> dict:
    if ref is None or ref <= 0:
        return {
            "ref_price": None, "suggest_limit": None, "buy_band": None,
        }
    slip = slip_bps / 10000.0
    band = band_pct / 100.0
    limit = round(ref * (1.0 + slip), 4)
    lo = round(ref * (1.0 - band), 4)
    hi = round(ref * (1.0 + band), 4)
    return {
        "ref_price": round(ref, 4),
        "suggest_limit": limit,
        "buy_band": [lo, hi],
    }


def _lot_hint(target_w: float, cash: float | None, ref: float | None) -> dict:
    if cash is None or cash <= 0 or ref is None or ref <= 0 or target_w <= 0:
        return {"shares_hint": None, "notional_hint": None}
    notional = cash * target_w
    shares = int(notional // ref // 100) * 100
    if shares <= 0:
        return {"shares_hint": 0, "notional_hint": 0.0}
    return {
        "shares_hint": shares,
        "notional_hint": round(shares * ref, 2),
    }


def pick_strategy(
    spec: StrategyRunSpec,
    codes: list[str],
    as_of: date,
    *,
    max_gross_override: float | None = None,
    write_cache: bool = False,
) -> dict[str, Any]:
    """单策略空仓重建选股。"""
    discover_and_register()
    discover_rebalancers()

    # 策略 config 从 DB 读(非 yaml 文件)——变体 base#key 无独立文件,其 config 由
    # seed._expand_variants 合成落库;与回测 get_active_strategy 同一真源。
    from stockfu.models import Strategy
    with session_scope() as s:
        row = s.get(Strategy, spec.strategy_id)
        if row is None:
            raise ValueError(
                f"策略 {spec.strategy_id} 不在 DB(先 --init-db / seed);"
                f"可选: {available_strategies()}"
            )
        yaml_text = row.config
    cs = compile_strategy(yaml_text, strategy_id=spec.strategy_id)
    deb = cs.debounce_params

    print(
        f"\n[{spec.strategy_id}] rebalancer={spec.rebalancer_id}  "
        f"universe={spec.universe}  n={len(codes)}",
        flush=True,
    )
    results = _analyze_all(cs, codes, as_of, write_cache=write_cache)

    desired: dict[str, float | None] = {}
    meta: dict[str, dict] = {}
    opinions_by: dict[str, list] = {}

    for code in codes:
        r = results.get(code) or {}
        if r.get("error") or not r.get("aggregate"):
            continue
        agg = r["aggregate"]
        total = agg.get("total_score")
        conf = agg.get("confidence")
        risk = agg.get("risk_vetoed", False)
        tw = compute_target_weight(
            risk, 0.0, agg.get("ai_target_weight"),
            total_score=total,
            max_w=deb.max_weight,
            dead=deb.total_dead,
            score_full=deb.score_full,
        )
        if tw is None or tw <= 0:
            continue
        desired[code] = tw
        meta[code] = {
            "score": total,
            "confidence": conf,
            "signal": agg.get("final_signal"),
            "risk_vetoed": risk,
            "raw": total,
        }
        opinions_by[code] = r.get("opinions") or []

    current = {c: 0.0 for c in desired}
    rb_cls = get_rebalancer(spec.rebalancer_id)
    if rb_cls is None:
        raise ValueError(f"未知 rebalancer: {spec.rebalancer_id}")
    rb = rb_cls()
    params = dict(spec.rebalancer_params or {})
    if max_gross_override is not None:
        params["max_gross"] = max_gross_override
    final = rb.adjust(desired, current, meta, equity=1_000_000.0, params=params)

    picks: list[dict] = []
    for code, tw in final.items():
        if tw is None or tw <= 1e-6:
            continue
        m = meta.get(code) or {}
        op_map = {o.get("advisor"): o for o in opinions_by.get(code) or []}
        factor_scores = {
            k: (op_map[k].get("score") if k in op_map else None)
            for k in op_map
        }
        picks.append({
            "code": code,
            "target_w": round(float(tw), 4),
            "desired_w": round(float(desired.get(code) or 0), 4),
            "score": m.get("score"),
            "confidence": m.get("confidence"),
            "signal": m.get("signal"),
            "factors": factor_scores,
            "opinions": opinions_by.get(code) or [],
        })
    picks.sort(key=lambda x: (-(x["target_w"] or 0), -(x["score"] or -999), x["code"]))
    gross = sum(p["target_w"] for p in picks)

    cfg = yaml.safe_load(yaml_text) or {}
    return {
        "strategy_id": spec.strategy_id,
        "strategy_name": cfg.get("name") or spec.strategy_id,
        "rebalancer_id": spec.rebalancer_id,
        "rebalancer_params": params,
        "universe": spec.universe,
        "as_of": as_of.isoformat(),
        "n_universe": len(codes),
        "n_scored_positive": len(desired),
        "n_picks": len(picks),
        "gross": round(gross, 4),
        "score_full": deb.score_full,
        "max_w": deb.max_weight,
        "picks": picks,
        "backtest": _backtest_meta_for(spec.strategy_id),
    }


def enrich_picks(
    picks: list[dict],
    as_of: date,
    *,
    names: dict[str, str],
    rets: dict[str, dict],
    sent: dict[str, dict],
    cash: float | None,
    slip_bps: float,
    band_pct: float,
    with_valuation: bool = True,
) -> list[dict]:
    for p in picks:
        c = p["code"]
        p["name"] = names.get(c, "")
        r = rets.get(c) or {}
        s = sent.get(c) or {}
        close = r.get("close")
        p["d1"] = r.get("d1")
        p["w1"] = r.get("w1")
        p["m1"] = r.get("m1")
        p["y1"] = r.get("y1")
        p["fear"] = s.get("fear")
        p["greed"] = s.get("greed")
        p["heat"] = s.get("heat")
        p.update(_exec_prices(close, slip_bps, band_pct))
        p.update(_lot_hint(float(p.get("target_w") or 0), cash, p.get("ref_price")))
        p["risk_flags"] = list(p.get("risk_flags") or [])
        if with_valuation:
            vs = valuation_snapshot(c, as_of, close=close)
            p["pe"] = vs.get("pe")
            p["pb"] = vs.get("pb")
            p["pe_pct"] = vs.get("pe_pct")
            p["pb_pct"] = vs.get("pb_pct")
            p["fair_price_pe"] = vs.get("fair_price_pe")
            p["fair_price_pb"] = vs.get("fair_price_pb")
            p["value_zone"] = vs.get("value_zone")
            p["value_band"] = vs.get("value_band")
            if vs.get("value_zone") == "rich":
                p["risk_flags"].append("valuation_rich")
            if vs.get("value_zone") == "unknown":
                p["risk_flags"].append("valuation_unknown")
        # 不落盘超大 opinions 可保留;调用方可剥
    return picks


def build_consensus(strategy_reports: list[dict]) -> list[dict]:
    """多策略交集:按入选策略数降序,同分用均分。"""
    if len(strategy_reports) < 2:
        return []
    by_code: dict[str, dict] = {}
    for rep in strategy_reports:
        sid = rep["strategy_id"]
        for p in rep.get("picks") or []:
            c = p["code"]
            slot = by_code.setdefault(c, {
                "code": c,
                "name": p.get("name") or "",
                "strategy_ids": [],
                "scores": [],
                "target_ws": [],
            })
            slot["strategy_ids"].append(sid)
            if p.get("score") is not None:
                slot["scores"].append(p["score"])
            if p.get("target_w") is not None:
                slot["target_ws"].append(p["target_w"])
            if p.get("name"):
                slot["name"] = p["name"]
    out = []
    for c, slot in by_code.items():
        n = len(slot["strategy_ids"])
        if n < 2:
            continue
        avg_score = (
            round(sum(slot["scores"]) / len(slot["scores"]), 2)
            if slot["scores"] else None
        )
        avg_w = (
            round(sum(slot["target_ws"]) / len(slot["target_ws"]), 4)
            if slot["target_ws"] else None
        )
        out.append({
            "code": c,
            "name": slot["name"],
            "n_strategies": n,
            "strategy_ids": sorted(slot["strategy_ids"]),
            "avg_score": avg_score,
            "avg_target_w": avg_w,
        })
    out.sort(key=lambda x: (-x["n_strategies"], -(x["avg_score"] or -999), x["code"]))
    return out


def run_recommend(
    strategies: Iterable[str],
    as_of: date | str | None = None,
    *,
    cash: float | None = 1_000_000.0,
    slip_bps: float = 10.0,
    band_pct: float = 1.0,
    max_gross: float | None = None,
    min_amount: float | None = None,
    with_sentiment: bool = False,
    write_cache: bool = False,
    save: bool = True,
) -> dict[str, Any]:
    """主入口:空仓重建荐股报告。"""
    if as_of is None:
        as_of_d = default_as_of()
    elif isinstance(as_of, str):
        as_of_d = date.fromisoformat(as_of[:10])
    else:
        as_of_d = as_of

    specs = resolve_strategy_specs(strategies)
    exec_d, exec_src = next_trade_date(as_of_d)

    discover_and_register()
    discover_rebalancers()

    names = _names()
    reports: list[dict] = []
    # 每策略可能宇宙不同(etf vs all)
    for spec in specs:
        codes = resolve_universe(spec, as_of_d, min_amount_override=min_amount)
        print(
            f"宇宙 {spec.strategy_id}: {len(codes)} 只 "
            f"(universe={spec.universe}, as_of={as_of_d})",
            flush=True,
        )
        if len(codes) < 5:
            print("  WARN: 宇宙过小,检查 as_of 是否有行情", flush=True)
        rep = pick_strategy(
            spec, codes, as_of_d,
            max_gross_override=max_gross,
            write_cache=write_cache,
        )
        reports.append(rep)

    pick_codes = sorted({p["code"] for r in reports for p in r["picks"]})
    print(f"\nenrich {len(pick_codes)} picks…", flush=True)
    rets = _returns(pick_codes, as_of_d)
    if with_sentiment:
        print("offline sentiment…", flush=True)
        sent = _sentiment_offline(pick_codes)
    else:
        sent = {c: {} for c in pick_codes}

    for rep in reports:
        enrich_picks(
            rep["picks"], as_of_d,
            names=names, rets=rets, sent=sent,
            cash=cash, slip_bps=slip_bps, band_pct=band_pct,
            with_valuation=True,
        )

    consensus = build_consensus(reports)
    # consensus 补 name/ref 若已 enrich
    name_map = {p["code"]: p for r in reports for p in r["picks"]}
    for c in consensus:
        src = name_map.get(c["code"]) or {}
        c["name"] = c.get("name") or src.get("name") or ""
        c["ref_price"] = src.get("ref_price")
        c["suggest_limit"] = src.get("suggest_limit")
        c["value_zone"] = src.get("value_zone")
        c["fair_price_pe"] = src.get("fair_price_pe")

    sid_key = ",".join(s.strategy_id for s in specs)
    sid_hash = hashlib.sha1(sid_key.encode()).hexdigest()[:8]
    report: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mode": "greenfield",
        "signal_date": as_of_d.isoformat(),
        "exec_date": exec_d.isoformat() if exec_d else None,
        "exec_date_source": exec_src,
        "strategies_requested": [s.strategy_id for s in specs],
        "cash": cash,
        "slip_bps": slip_bps,
        "band_pct": band_pct,
        "exec_note": EXEC_NOTE,
        "strategies": reports,
        "consensus": consensus,
        "n_picks_total": sum(r["n_picks"] for r in reports),
    }

    if save:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = REPORT_DIR / f"{as_of_d.isoformat()}_{sid_hash}.json"
        out_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        report["saved_to"] = str(out_path)
        print(f"\nJSON → {out_path}", flush=True)

    return report


def print_report(report: dict) -> None:
    print(f"\n{'=' * 72}")
    print(
        f"荐股  signal={report['signal_date']}  exec={report.get('exec_date')} "
        f"({report.get('exec_date_source')})  mode={report['mode']}"
    )
    print(f"策略: {', '.join(report.get('strategies_requested') or [])}")
    print(f"注: {report.get('exec_note')}")
    for rep in report.get("strategies") or []:
        print(f"\n--- {rep.get('strategy_name')} ({rep['strategy_id']}) ---")
        bt = rep.get("backtest") or {}
        if bt:
            print(
                f"  回测: 收益 {bt.get('total_return_pct')}%  超额 {bt.get('excess_pct')}%  "
                f"回撤 {bt.get('max_drawdown_pct')}%  夏普 {bt.get('sharpe')}  "
                f"窗口 {bt.get('window')}  run={bt.get('run_id')}"
            )
        else:
            print("  回测: (无匹配 meta)")
        print(
            f"  宇宙={rep.get('n_universe')}  正分={rep.get('n_scored_positive')}  "
            f"入选={rep.get('n_picks')}  合计仓位={float(rep.get('gross') or 0) * 100:.1f}%"
        )
        print(
            f"{'#':>3} {'代码':8} {'名称':10} {'仓%':>6} {'得分':>7} "
            f"{'参考价':>8} {'限价':>8} {'PE中枢':>8} {'区':>6} {'PE%':>5}"
        )
        for i, p in enumerate(rep.get("picks") or [], 1):
            def g(v, nd=2):
                return f"{v:.{nd}f}" if v is not None else "—"

            print(
                f"{i:3d} {p['code']:8} {(p.get('name') or '')[:10]:10} "
                f"{float(p.get('target_w') or 0) * 100:6.2f} "
                f"{g(p.get('score'), 1):7} "
                f"{g(p.get('ref_price')):8} {g(p.get('suggest_limit')):8} "
                f"{g(p.get('fair_price_pe')):8} "
                f"{(p.get('value_zone') or '—'):>6} {g(p.get('pe_pct'), 0):5}"
            )

    cons = report.get("consensus") or []
    if cons:
        print(f"\n共识(≥2 策略, {len(cons)} 只):")
        for c in cons:
            print(
                f"  {c['code']} {(c.get('name') or '')[:8]}  "
                f"n={c['n_strategies']}  avg_score={c.get('avg_score')}  "
                f"strats={','.join(c.get('strategy_ids') or [])}"
            )
    if report.get("saved_to"):
        print(f"\n已保存: {report['saved_to']}")
