"""通用股票评价引擎(decoupled evaluator)。

设计核心 —— **完全解耦**：评价框架是通用引擎，三个输入全是纯参数，互不绑定::

    evaluate(codes, strategy_ids, as_of) -> report

  - 股票池与策略解耦:codes 是任意 list[str];watchlist/active 装配只在入口层
    (run_watchlist_review / CLI),核心函数不读 watchlist、不读 active_strategy。
  - 策略与股票池解耦:strategy_ids 是任意 list[str],从 DB strategy 表加载 yaml,
    不限 FULL_CYCLE_CATALOG。
  - 不做横截面排名(那是「选股」,不是「评价」);推荐度直接用 ai_target_weight /
    total_score 归一化,逐股逐策略一格 + 综合共识。

与 recommend.py 的区别:
  - recommend.pick_strategy = greenfield 空仓重建,只留 tw>0 的入选票,严格 catalog。
  - 本模块 = 全量点评矩阵(信号弱的也列),任意入库策略,纯参数入。

不做(明确边界):
  - 不做持仓调仓 / 组合风控干预(regime 只作环境提示,见 market_regime_hint)。
  - 不抽取 engine.py 风控函数(只 import 用一次)。
"""
from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from sqlmodel import select

from stockfu.ai.operators.registry import discover_and_register
from stockfu.ai.operators.runner import compile_strategy
from stockfu.db import session_scope
from stockfu.models import Strategy

ROOT = Path(__file__).resolve().parents[2]
REPORT_DIR = ROOT / "data" / "reports" / "evaluator"

# 信号 → 数值优先级(用于跨策略多数票 / 排序)。数值越大越看多。
_SIGNAL_RANK = {
    "strong_buy": 3, "buy": 2, "hold": 1, "sell": 0, "strong_sell": -1,
}


# ────────────────────────────────────────────────────────────────────
# 1. 策略加载
# ────────────────────────────────────────────────────────────────────

def available_strategy_ids() -> list[str]:
    """DB strategy 表全部 id(报错提示用)。"""
    with session_scope() as s:
        return sorted(r.strategy_id for r in s.exec(select(Strategy)).all())


def _load_strategies(strategy_ids: list[str]) -> list[tuple[str, str, str]]:
    """从 DB 加载策略 → [(strategy_id, name, yaml_text)]。

    - 去重保序(用户传入序)。
    - 未知 id 报 ValueError 并列出全部可选;空列表报错(调用方负责默认值)。
    """
    ids: list[str] = []
    seen: set[str] = set()
    for raw in strategy_ids:
        sid = (raw or "").strip()
        if sid and sid not in seen:
            seen.add(sid)
            ids.append(sid)
    if not ids:
        raise ValueError("strategy_ids 为空;请传入至少一个策略 id(或由调用方填默认 active)。")

    with session_scope() as s:
        rows = {r.strategy_id: r for r in s.exec(select(Strategy).where(
            Strategy.strategy_id.in_(ids))).all()}
    missing = [i for i in ids if i not in rows]
    if missing:
        raise ValueError(
            f"未知 strategy_id: {missing}; 可选: {available_strategy_ids()}"
        )
    return [(i, rows[i].name or i, rows[i].config) for i in ids]


# ────────────────────────────────────────────────────────────────────
# 2. 单策略评价(全量,含信号弱/风险否决/error)
# ────────────────────────────────────────────────────────────────────

def _eval_one_strategy(
    strategy_id: str, yaml_text: str, codes: list[str], as_of: date,
    *, write_cache: bool = False,
) -> dict[str, dict]:
    """编译策略 → 对每个 code 跑 analyze → 全量返回 {code: cell}。

    cell 结构:
      {signal, total_score, confidence, ai_target_weight, factors:{op:score},
       risk_vetoed, opinions:[...], error?}
    analyze 抛错 → cell={"error": str, "signal": "error"},不阻断其他 code。
    编译失败 → 返回 {"__compile_error__": str}(调用方识别后整列标错)。
    """
    discover_and_register()
    try:
        cs = compile_strategy(yaml_text, strategy_id=strategy_id)
    except Exception as e:  # 整策略编译失败:不阻断其他策略
        return {"__compile_error__": f"{type(e).__name__}: {e}"}

    results = _analyze_all(cs, codes, as_of, write_cache=write_cache)

    cells: dict[str, dict] = {}
    for code in codes:
        r = results.get(code) or {}
        if r.get("error"):
            cells[code] = {"error": r["error"], "signal": "error"}
            continue
        agg = r.get("aggregate") or {}
        op_map = {o.get("advisor"): o for o in (r.get("opinions") or [])}
        cells[code] = {
            "signal": agg.get("final_signal"),
            "total_score": agg.get("total_score"),
            "confidence": agg.get("confidence"),
            "ai_target_weight": agg.get("ai_target_weight"),
            "risk_vetoed": agg.get("risk_vetoed", False),
            "factors": {
                k: (v.get("score") if isinstance(v, dict) else v)
                for k, v in op_map.items()
            },
            "opinions": r.get("opinions") or [],
        }
    return cells


def _analyze_all(cs, codes: list[str], as_of: date, write_cache: bool = False) -> dict[str, dict]:
    """缓存优先;默认 miss 不写库(避免与回测抢 operator_cache 写锁)。

    复用 recommend._analyze_all 的取数 + 容错思路,但本模块独立实现以保持解耦
    (evaluator 不依赖 recommend.py)。
    """
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
            if (i + 1) % 50 == 0:
                print(f"  analyze {i + 1}/{len(codes)}  {time.time() - t0:.0f}s", flush=True)
    finally:
        oc.save_operator_result = _orig_save
        oc.save_operator_results_batch = _orig_batch
    return results


# ────────────────────────────────────────────────────────────────────
# 3. 多策略矩阵 + 综合共识
# ────────────────────────────────────────────────────────────────────

def build_matrix(
    per_strategy: list[dict], codes: list[str],
) -> list[dict]:
    """行=每股,列=各策略 cell + 共识。全集聚合(区别于 recommend.build_consensus
    只统计 n_strategies≥2 的交集 —— 评价场景每只票都要有结论)。

    per_strategy = [{"strategy_id", "name", "cells": {code: cell}, "error?}, ...]
    返回 [{"code", "per_strategy": {sid: cell}, "consensus": {...}}, ...]
    按 consensus 推荐度降序(buy 多靠前,sell 多靠后)。
    """
    sids = [s["strategy_id"] for s in per_strategy]
    rows: list[dict] = []
    for code in codes:
        per: dict[str, dict] = {}
        for s in per_strategy:
            sid = s["strategy_id"]
            if s.get("error"):
                per[sid] = {"error": s["error"], "signal": "error"}
            else:
                per[sid] = (s.get("cells") or {}).get(code) or {
                    "signal": None, "total_score": None,
                }
        rows.append({"code": code, "per_strategy": per, "consensus": _consensus(per, sids)})
    rows.sort(key=lambda r: (-_consensus_rank(r["consensus"]), r["code"]))
    return rows


def _consensus(per: dict[str, dict], sids: list[str]) -> dict[str, Any]:
    """跨策略多数票 + 均分 + 一致性。error/None 不参与投票但计入分母(视为无信号)。"""
    votes: list[str] = []
    scores: list[float] = []
    n_buy = n_hold = n_sell = n_error = 0
    for sid in sids:
        c = per.get(sid) or {}
        sig = c.get("signal")
        if c.get("error") or sig is None:
            n_error += 1
            continue
        votes.append(sig)
        sc = c.get("total_score")
        if sc is not None:
            scores.append(float(sc))
        if sig in ("buy", "strong_buy"):
            n_buy += 1
        elif sig == "hold":
            n_hold += 1
        elif sig in ("sell", "strong_sell"):
            n_sell += 1

    consensus_signal = _majority_signal(votes) if votes else None
    avg_score = round(sum(scores) / len(scores), 2) if scores else None
    # 一致性 = 最高票数 / 有效投票数(0~1;1=全策略同信号)
    agreement: float | None = None
    if votes:
        from collections import Counter
        top = Counter(votes).most_common(1)[0][1]
        agreement = round(top / len(votes), 2)
    return {
        "signal": consensus_signal,
        "avg_score": avg_score,
        "n_buy": n_buy, "n_hold": n_hold, "n_sell": n_sell, "n_error": n_error,
        "n_strategies": len(sids),
        "agreement": agreement,
    }


def _majority_signal(votes: list[str]) -> str:
    """多数票:看多(buy/strong_buy) vs 中性(hold) vs 看空(sell/strong_sell) 三类计数,
    取最多类;同类内取较强者(buy>strong_buy? 否,取更激进:strong_buy>buy)。
    平票时倾向中性(保守)。"""
    n_buy = sum(1 for v in votes if v in ("buy", "strong_buy"))
    n_sell = sum(1 for v in votes if v in ("sell", "strong_sell"))
    n_hold = sum(1 for v in votes if v == "hold")
    if n_buy > n_hold and n_buy > n_sell:
        return "strong_buy" if "strong_buy" in votes else "buy"
    if n_sell > n_hold and n_sell > n_buy:
        return "strong_sell" if "strong_sell" in votes else "sell"
    return "hold"


def _consensus_rank(c: dict[str, Any]) -> int:
    """共识 → 排序键(降序):信号优先级 → n_buy → avg_score。"""
    sig = c.get("signal")
    base = _SIGNAL_RANK.get(sig, 0) * 1000
    return base + c.get("n_buy", 0) * 10 + int((c.get("avg_score") or 0))


# ────────────────────────────────────────────────────────────────────
# 4. 主入口:evaluate()(纯参数,解耦核心)
# ────────────────────────────────────────────────────────────────────

def evaluate(
    codes: list[str],
    strategy_ids: list[str],
    as_of: date,
    *,
    write_cache: bool = False,
) -> dict[str, Any]:
    """通用评价:codes × strategy_ids 矩阵。

    纯参数入,不读 watchlist / active_strategy / app_config —— 解耦核心。
    富集(name/returns/估值/LLM 点评/regime 提示)在 run_watchlist_review 装配层做,
    保持本函数快、纯、可测。

    返回:
      {"as_of", "strategy_ids", "codes", "strategies":[{strategy_id,name,cells,error?}],
       "matrix":[{code, per_strategy, consensus}], "n_codes", "n_strategies"}
    """
    codes = sorted({(c or "").strip() for c in codes if c and str(c).strip()})
    if not codes:
        raise ValueError("codes 为空;请传入待评价股票代码列表。")

    loaded = _load_strategies(strategy_ids)

    strategies_out: list[dict] = []
    for sid, name, yaml_text in loaded:
        print(f"\n[{sid}] {name}  n_codes={len(codes)}  as_of={as_of}", flush=True)
        cells = _eval_one_strategy(sid, yaml_text, codes, as_of, write_cache=write_cache)
        if "__compile_error__" in cells:
            print(f"  编译失败: {cells['__compile_error__']}", flush=True)
            strategies_out.append({
                "strategy_id": sid, "name": name,
                "error": cells["__compile_error__"], "cells": {},
            })
        else:
            n_ok = sum(1 for c in cells.values() if not c.get("error"))
            print(f"  done: {n_ok}/{len(codes)} OK", flush=True)
            strategies_out.append({
                "strategy_id": sid, "name": name, "cells": cells,
            })

    matrix = build_matrix(strategies_out, codes)
    return {
        "as_of": as_of.isoformat(),
        "strategy_ids": [s["strategy_id"] for s in strategies_out],
        "strategy_names": {s["strategy_id"]: s["name"] for s in strategies_out},
        "codes": codes,
        "strategies": strategies_out,
        "matrix": matrix,
        "n_codes": len(codes),
        "n_strategies": len(strategies_out),
    }


# ────────────────────────────────────────────────────────────────────
# 5. LLM 点评(per code,可选)
# ────────────────────────────────────────────────────────────────────

def narrate_review(
    code: str, name: str, per_strategy: dict[str, dict],
    consensus: dict[str, Any], strategy_names: dict[str, str],
) -> tuple[str | None, str | None]:
    """LLM 把「多策略视角 + 共识」写成一段散户可读点评(2–4 句)。

    约束(沿用 synthesis.narrate 确定性原则):
      - 不得推翻 consensus.signal(数字已确定,LLM 只表达);
      - 不得给具体价位 / 目标价;
      - 基于各策略 cell 的 signal + factors 客观描述。
    返回 (narrative, error);失败时 narrative=None、error 填异常信息。
    """
    from stockfu.ai.client import chat, LLMError

    # 拼各策略视角(只取信号 + 因子分,不带原始 reasoning 以控 token)
    views: list[str] = []
    for sid, cell in per_strategy.items():
        sname = strategy_names.get(sid, sid)
        if cell.get("error") or cell.get("signal") is None:
            views.append(f"- {sname}: 无有效信号")
            continue
        sig = cell.get("signal")
        score = cell.get("total_score")
        factors = cell.get("factors") or {}
        fac_str = ", ".join(f"{k}={v}" for k, v in factors.items() if v is not None)
        views.append(f"- {sname}: 信号={sig}, 总分={score}{(';' + fac_str) if fac_str else ''}")

    cons = consensus or {}
    cons_str = (
        f"共识信号={cons.get('signal')}, 均分={cons.get('avg_score')}, "
        f"看多{cons.get('n_buy')}/中性{cons.get('n_hold')}/看空{cons.get('n_sell')}, "
        f"一致性={cons.get('agreement')}"
    )

    prompt = (
        f"你是资深A股分析师,用散户听得懂的话点评一只股票,2-4句。\n"
        f"股票:{name}({code})\n"
        f"多策略评价:\n" + "\n".join(views) + "\n\n"
        f"综合共识:{cons_str}\n\n"
        f"硬约束:\n"
        f"1. 不得推翻上述共识信号;\n"
        f"2. 不得给出任何具体价位或目标价;\n"
        f"3. 客观描述各策略分歧,不要编造未给出的信息;\n"
        f"4. 直接输出点评正文,不要前后缀。"
    )
    try:
        text = chat(
            [{"role": "user", "content": prompt}],
            temperature=0.2, max_tokens=320, timeout=60.0,
        )
        return (text.strip() or None), None
    except (LLMError, Exception) as e:
        return None, f"{type(e).__name__}: {e}"


# ────────────────────────────────────────────────────────────────────
# 6. 大盘 regime 环境提示(不干预,纯信息)
# ────────────────────────────────────────────────────────────────────

def market_regime_hint(as_of: date, strategy_yaml_text: str | None = None) -> dict[str, Any]:
    """读 CSI300 收盘序列,跑一次 _market_throttle_step 算 bear_latched + vol cap。

    参数取自 active 策略 yaml 的 risk.market_regime_*;yaml 缺省时用 engine 常量。
    纯信息性 —— 不改变 evaluate() 的评价结果(与回测不同,回测里 regime 会压敞口)。
    返回 {text, bear_latched, vol_cap, source}。
    """
    from datetime import timedelta
    from stockfu.backtest.engine import (
        BENCHMARK, _market_throttle_step, _preload_bench_closes, _bench_closes_asof,
    )

    # regime 参数:优先 active 策略 yaml,缺省回退 engine 默认
    rk: dict = {}
    if strategy_yaml_text:
        try:
            rk = (yaml.safe_load(strategy_yaml_text) or {}).get("risk") or {}
        except Exception:
            rk = {}
    code = rk.get("market_regime_code", BENCHMARK)
    ma_days = rk.get("market_regime_ma_days", 200)
    enter_band = rk.get("market_regime_enter_band", 0.0)
    exit_band = rk.get("market_regime_exit_band", 0.03)
    bear_gross = rk.get("market_regime_max_gross", 0.50)
    target_vol = rk.get("market_regime_target_vol", 0.15)
    vol_window = rk.get("market_regime_vol_window", 63)
    vol_floor = rk.get("market_regime_vol_floor", 0.30)
    max_gross = rk.get("max_gross", 0.90)

    start = as_of - timedelta(days=int(ma_days) * 2 + 60)  # 足够覆盖 MA + vol 窗口
    try:
        pre = _preload_bench_closes(code, start, as_of)
        win = _bench_closes_asof(pre, as_of, max(int(ma_days or 0), vol_window + 1, 252))
    except Exception as e:
        return {"text": f"大盘环境读取失败({type(e).__name__})", "bear_latched": None,
                "vol_cap": None, "source": "error"}

    # 恢复 bear_latched:用历史窗口重放进/出逻辑(无状态近似)
    bear_latched = _replay_bear_latch(
        win, ma_days=ma_days, enter_band=enter_band, exit_band=exit_band,
    )
    try:
        cap, _ = _market_throttle_step(
            win, bear_latched=bear_latched, ma_days=ma_days, enter_band=enter_band,
            exit_band=exit_band, bear_gross=bear_gross, target_vol=target_vol,
            vol_window=vol_window, vol_floor=vol_floor, max_gross=max_gross,
        )
    except Exception as e:
        return {"text": f"大盘环境计算失败({type(e).__name__})", "bear_latched": bear_latched,
                "vol_cap": None, "source": "error"}

    if bear_latched:
        text = f"⚠ 大盘趋势偏弱({code} 跌破 {ma_days}MA),新开仓建议谨慎"
    elif cap < max_gross - 1e-9:
        text = f"大盘波动偏高(敞口参考 {cap:.0%}),注意仓位"
    else:
        text = "大盘趋势正常"
    return {"text": text, "bear_latched": bear_latched, "vol_cap": cap, "source": "info"}


def _replay_bear_latch(
    bench_window: list[float], *, ma_days: int, enter_band: float, exit_band: float,
) -> bool:
    """从窗口起点逐日重放 _market_throttle_step 的进/出滞回逻辑,返回末日 latch 态。

    纯函数无副作用 —— 用于单次 live 调用恢复跨日累积的 bear_latched 状态。
    样本不足 → False(不拦)。
    """
    if not ma_days or ma_days <= 0 or len(bench_window) < max(5, ma_days // 4):
        return False
    latched = False
    for end in range(1, len(bench_window) + 1):
        seg = bench_window[:end]
        px = seg[-1]
        if px <= 0:
            continue
        w = min(ma_days, len(seg))
        ma = sum(seg[-w:]) / w
        if not latched and px < ma * (1.0 - enter_band):
            latched = True
        elif latched and px > ma * (1.0 + exit_band):
            latched = False
    return latched


# ────────────────────────────────────────────────────────────────────
# 7. 富集 + 打印 + 落盘(装配层辅助)
# ────────────────────────────────────────────────────────────────────

def enrich_matrix(
    matrix: list[dict], as_of: date, *,
    names: dict[str, str], rets: dict[str, dict], sent: dict[str, dict],
) -> list[dict]:
    """给矩阵每行补 name / returns / 估值区位(复用 valuation_snapshot)。

    与 recommend.enrich_picks 不同:不加执行价/lot hint(评价场景不是下单)。
    """
    from stockfu.services.valuation import valuation_snapshot
    for row in matrix:
        c = row["code"]
        row["name"] = names.get(c, "")
        r = rets.get(c) or {}
        s = sent.get(c) or {}
        close = r.get("close")
        row["d1"] = r.get("d1")
        row["w1"] = r.get("w1")
        row["m1"] = r.get("m1")
        row["y1"] = r.get("y1")
        row["fear"] = s.get("fear")
        row["greed"] = s.get("greed")
        row["heat"] = s.get("heat")
        try:
            vs = valuation_snapshot(c, as_of, close=close)
        except Exception:
            vs = {}
        row["pe"] = vs.get("pe")
        row["pe_pct"] = vs.get("pe_pct")
        row["value_zone"] = vs.get("value_zone")
    return matrix


def _sig_emoji(sig: str | None) -> str:
    return {
        "strong_buy": "🟢强买", "buy": "📗买入", "hold": "🟡持有",
        "sell": "📙卖出", "strong_sell": "🔴强卖", "error": "⚠错误",
    }.get(sig or "", "—" + str(sig or "") + "—") if sig else "—"


def print_matrix(report: dict, *, regime: dict | None = None) -> None:
    """表格输出:每行一只股,列=code|名称|[各策略 信号/得分]|共识|一致性|narrative首句。"""
    print(f"\n{'=' * 90}")
    print(f"自选股评价  signal={report.get('as_of')}  "
          f"策略={', '.join(report.get('strategy_ids') or [])}")
    if regime:
        print(f"大盘环境: {regime.get('text')}  (此为信息提示,不改变下方评价)")
    # 各策略回测 meta
    from stockfu.services.recommend import _backtest_meta_for
    for sid in report.get("strategy_ids") or []:
        bt = _backtest_meta_for(sid)
        if bt:
            print(f"  [{sid}] 回测: 收益 {bt.get('total_return_pct')}%  "
                  f"超额 {bt.get('excess_pct')}%  回撤 {bt.get('max_drawdown_pct')}%  "
                  f"夏普 {bt.get('sharpe')}  ({bt.get('window')})")
    print("-" * 90)

    sids = report.get("strategy_ids") or []
    header = f"{'code':<8} {'名称':<8} " + " ".join(f"{sid[:14]:<16}" for sid in sids) + \
             f" {'共识':<8} {'一致':<5} 点评"
    print(header)
    print("-" * 90)
    for row in report.get("matrix") or []:
        code = row["code"]
        name = (row.get("name") or "")[:4]
        cells = []
        for sid in sids:
            c = (row.get("per_strategy") or {}).get(sid) or {}
            sig = c.get("signal")
            sc = c.get("total_score")
            cells.append(f"{(sig or '?')[:6]:<7}{('_'+str(sc)) if sc is not None else '':<7}"[:16])
        cons = row.get("consensus") or {}
        cons_sig = cons.get("signal") or "—"
        agr = cons.get("agreement")
        nar = (row.get("narrative") or "").split("。")[0][:30]
        print(f"{code:<8} {name:<8} " + " ".join(f"{c:<16}" for c in cells) +
              f" {cons_sig:<8} {agr if agr is not None else '—':<5} {nar}")
    print("=" * 90)


# ────────────────────────────────────────────────────────────────────
# 8. 装配层入口:run_watchlist_review(这里才碰 watchlist / active_strategy)
# ────────────────────────────────────────────────────────────────────

def assemble_codes(
    pool_spec: str = "watchlist",
    *,
    codes_override: list[str] | None = None,
    add: list[str] | None = None,
    drop: list[str] | None = None,
) -> list[str]:
    """股票池装配(装配层,非引擎):base 按池类型解析,再 (base ∪ add) − drop。

    **不写 DB、不改 watchlist 表**(临时增删)。
    codes_override 非空时直接用显式列表(绕开 pool_spec/add/drop)。

    注意:watchlist 走真正的 is_watch=True 查询(用户用 --watch 管理的自选池),
    **不**用 resolve_base_codes("watchlist")——后者有 bug:文档说「自选」但实现
    返回 market=cn 全部 Asset(含 is_watch=False),与「自选」语义不符。
    其它池(all/historical_indices/显式列表)仍走 resolve_base_codes。
    """
    from stockfu.services.universe import resolve_base_codes

    if codes_override:
        return sorted({c.strip() for c in codes_override if c and str(c).strip()})

    spec = (pool_spec or "watchlist").strip()
    if spec.lower() in ("watchlist", "watch", "self"):
        base = _true_watchlist_codes()
    else:
        base = resolve_base_codes(spec)
    pool = set(base)
    if add:
        pool |= {normalize_stock_code(c) for c in add if c}
    if drop:
        pool -= {normalize_stock_code(c) for c in drop if c}
    return sorted(c for c in pool if c)


def _true_watchlist_codes() -> list[str]:
    """真正的自选池:Asset.is_watch=True(用户 --watch 管理的)。

    与 resolve_base_codes("watchlist")(返回 market=cn 全部)区分;本函数才是
    「自选股」的真语义。
    """
    from stockfu.models import Asset
    with session_scope() as s:
        return sorted(a.code for a in s.exec(
            select(Asset).where(Asset.is_watch == True)  # noqa: E712
        ).all() if a.code)


def normalize_stock_code(code: str) -> str:
    """轻量规范化(与 trading.normalize_stock_code 一致行为:去空白)。
    不 import trading 以免引入循环依赖;复杂规范化在调用方做。"""
    return (code or "").strip()


def default_strategy_id() -> str:
    """active_strategy_id(DB app_config);缺省 pure_factor。"""
    from stockfu.db import get_app_config, has_app_config
    if has_app_config("active_strategy_id"):
        v = get_app_config("active_strategy_id", "").strip()
        if v:
            return v
    return "pure_factor"


def run_watchlist_review(
    *,
    pool_spec: str = "watchlist",
    codes_override: list[str] | None = None,
    add: list[str] | None = None,
    drop: list[str] | None = None,
    strategies: list[str] | None = None,
    as_of: date | str | None = None,
    with_sentiment: bool = False,
    with_llm: bool = True,
    write_cache: bool = False,
    save: bool = True,
) -> dict[str, Any]:
    """装配层入口:装配股票池 + 策略 → evaluate() → 富集 + LLM + regime + 打印 + 落盘。

    策略:strategies 为空 → 默认 [active_strategy_id]。
    as_of:None → DB 最新交易日(recommend.default_as_of)。
    """
    from stockfu.services.recommend import (
        _names, _returns, _sentiment_offline, default_as_of,
    )

    # 1. as_of
    if as_of is None:
        as_of_d = default_as_of()
    elif isinstance(as_of, str):
        as_of_d = date.fromisoformat(as_of[:10])
    else:
        as_of_d = as_of

    # 2. 装配股票池
    codes = assemble_codes(pool_spec, codes_override=codes_override, add=add, drop=drop)
    if not codes:
        print("⚠ 评价池为空:watchlist 无自选股,或 --codes/--add 未提供有效代码。"
              "\n  先用 `python main.py --watch CODE` 添加自选,或用 --codes 显式指定。")
        return {"error": "empty_pool", "as_of": as_of_d.isoformat()}

    # 3. 装配策略
    sids = list(strategies or [])
    if not sids:
        sids = [default_strategy_id()]

    # 4. 跑引擎(纯参数)
    report = evaluate(codes, sids, as_of_d, write_cache=write_cache)

    # 5. regime 环境提示(用第一个策略 yaml 的 risk;不干预评价)
    regime = None
    first_yaml = None
    with session_scope() as s:
        row = s.get(Strategy, report["strategy_ids"][0]) if report["strategy_ids"] else None
        if row:
            first_yaml = row.config
    try:
        regime = market_regime_hint(as_of_d, strategy_yaml_text=first_yaml)
    except Exception as e:
        regime = {"text": f"大盘环境读取失败({type(e).__name__})", "source": "error"}
    report["regime"] = regime

    # 6. 富集
    print(f"\nenrich {len(codes)} codes…", flush=True)
    names = _names()
    rets = _returns(codes, as_of_d)
    if with_sentiment:
        print("offline sentiment…", flush=True)
        sent = _sentiment_offline(codes)
    else:
        sent = {c: {} for c in codes}
    report["matrix"] = enrich_matrix(
        report["matrix"], as_of_d, names=names, rets=rets, sent=sent,
    )

    # 7. LLM 点评(per code,可选)
    if with_llm:
        print(f"\nLLM 点评 {len(report['matrix'])} 只…(失败的单只不阻断,--no-llm 可跳过)", flush=True)
        strat_names = report.get("strategy_names") or {}
        t0 = time.time()
        for i, row in enumerate(report["matrix"]):
            nar, err = narrate_review(
                row["code"], row.get("name") or "", row.get("per_strategy") or {},
                row.get("consensus") or {}, strat_names,
            )
            row["narrative"] = nar
            if err:
                row["narrative_error"] = err
            if (i + 1) % 10 == 0:
                print(f"  narrate {i + 1}/{len(report['matrix'])}  {time.time() - t0:.0f}s", flush=True)
        n_ok = sum(1 for r in report["matrix"] if r.get("narrative"))
        print(f"  narrate done: {n_ok}/{len(report['matrix'])} OK in {time.time() - t0:.0f}s", flush=True)

    report["mode"] = "watchlist_review"
    report["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    report["pool_spec"] = pool_spec
    report["add"] = list(add or [])
    report["drop"] = list(drop or [])

    # 8. 打印 + 落盘
    print_matrix(report, regime=regime)

    if save:
        import hashlib
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        sid_key = ",".join(report["strategy_ids"])
        sid_hash = hashlib.sha1(sid_key.encode()).hexdigest()[:8]
        out_path = REPORT_DIR / f"{as_of_d.isoformat()}_{sid_hash}.json"
        # 落盘前剥 opinions(体积大;opinions 保留在内存 report 里)
        slim = {k: v for k, v in report.items()}
        slim["matrix"] = []
        for row in report["matrix"]:
            sr = dict(row)
            ps = {}
            for sid, cell in (row.get("per_strategy") or {}).items():
                cc = dict(cell)
                cc.pop("opinions", None)
                ps[sid] = cc
            sr["per_strategy"] = ps
            slim["matrix"].append(sr)
        out_path.write_text(
            json.dumps(slim, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        report["saved_to"] = str(out_path)
        print(f"\nJSON → {out_path}", flush=True)

    return report

