"""全周期策略回测更新(固化能力)。

补完行情后,用与验收一致的口径整段重跑 start→数据末日:
  - 默认 start=2021-01-01(与 PROJECT_STATE §0.3 全周期表一致)
  - end 默认取库内个股/ETF 行情 max(quote_date)
  - 策略目录含 rebalancer/宇宙/strict(CLI --backtest 传不了 rebalancer 的缺口在此补齐)
  - 可选只跑部分 strategy_id;不选 = 目录全部

CLI:
  python3 main.py --update-backtests
  python3 main.py --update-backtests --strategies cross_section_factor,dividend_cross_section
  python3 main.py --update-backtests --list
  python3 main.py --update-backtests --dry-run
  python3 main.py --update-backtests --start 2021-01-01 --end 2026-07-20
"""
from __future__ import annotations

import json
import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from sqlmodel import select

# ── 全周期验收默认口径 ─────────────────────────────────────────────
DEFAULT_START = "2021-01-01"
MIN_AMOUNT = 50_000_000  # 个股动态池成交额门槛(元)

# top_n 轮动常用参数(与历史 run_*.py / 全周期表一致)
_TOP_N_STD = {"top_n": 8, "lock_days": 20, "max_replace": 1, "max_w": 0.12, "max_gross": 0.90}
_TOP_N_ETF = {"top_n": 5, "lock_days": 20, "max_replace": 1, "max_w": 0.18, "max_gross": 0.90}
_TOP_N_BOLL = {"top_n": 10, "lock_days": 20, "max_replace": 1, "max_w": 0.20, "max_gross": 0.90}
_TOP_N_MOM = {"top_n": 10, "lock_days": 20, "max_replace": 1, "max_w": 0.15, "max_gross": 0.90}
_CS = {"max_gross": 0.95}


@dataclass(frozen=True)
class StrategyRunSpec:
    """一条可全周期更新的策略规格(策略 + 选股层 + 宇宙)。"""
    strategy_id: str
    rebalancer_id: str
    rebalancer_params: dict = field(default_factory=dict)
    universe: str = "all"       # all | etf | universe_788
    strict: bool = True
    min_amount: float | None = MIN_AMOUNT  # 仅 universe=all 时生效; etf/788 为 None
    tier: str = "warm"          # hot / warm / cold — 批跑顺序


# 目录顺序: 先 hot(池小/缓存热) → warm → cold(布林冷补), 与历史批跑习惯一致。
# 覆盖 seed 全量策略 + 全周期验收表口径;新增策略请同步登记。
FULL_CYCLE_CATALOG: list[StrategyRunSpec] = [
    # ── HOT ──────────────────────────────────────────────────────
    StrategyRunSpec(
        "etf_momentum_cross_section", "cap_and_rank", dict(_CS),
        universe="etf", strict=False, min_amount=None, tier="hot",
    ),
    StrategyRunSpec(
        "etf_momentum_rotation", "top_n_picker", dict(_TOP_N_ETF),
        universe="etf", strict=False, min_amount=None, tier="hot",
    ),
    StrategyRunSpec(
        "cn_momentum_cross_section", "cap_and_rank", dict(_CS),
        universe="all", strict=True, min_amount=MIN_AMOUNT, tier="hot",
    ),
    StrategyRunSpec(
        "reversal_cross_section", "cap_and_rank", dict(_CS),
        universe="all", strict=True, min_amount=MIN_AMOUNT, tier="hot",
    ),
    StrategyRunSpec(
        "cross_section_factor", "cap_and_rank", dict(_CS),
        universe="all", strict=True, min_amount=MIN_AMOUNT, tier="hot",
    ),
    StrategyRunSpec(
        "dividend_cross_section", "cap_and_rank", dict(_CS),
        universe="all", strict=True, min_amount=MIN_AMOUNT, tier="hot",
    ),
    # 变体:同一 base 不同参数并存(strategy_id=base#key,见 seed._expand_variants)。
    # sl30 = dividend_cross_section 止损 8%→30%(实证更优),与 base 并列全周期验收。
    StrategyRunSpec(
        "dividend_cross_section#sl30", "cap_and_rank", dict(_CS),
        universe="all", strict=True, min_amount=MIN_AMOUNT, tier="hot",
    ),
    StrategyRunSpec(
        "macd_cross", "top_n_picker", dict(_TOP_N_STD),
        universe="all", strict=True, min_amount=MIN_AMOUNT, tier="hot",
    ),
    # ── WARM ─────────────────────────────────────────────────────
    StrategyRunSpec(
        "momentum_breakout_cross_section", "cap_and_rank", dict(_CS),
        universe="all", strict=True, min_amount=MIN_AMOUNT, tier="warm",
    ),
    StrategyRunSpec(
        "momentum_breakout", "top_n_picker", dict(_TOP_N_MOM),
        universe="universe_788", strict=False, min_amount=None, tier="warm",
    ),
    StrategyRunSpec(
        "reversal_strategy", "top_n_picker", dict(_TOP_N_STD),
        universe="all", strict=True, min_amount=MIN_AMOUNT, tier="warm",
    ),
    StrategyRunSpec(
        "dividend_low_vol", "top_n_picker", dict(_TOP_N_STD),
        universe="all", strict=True, min_amount=MIN_AMOUNT, tier="warm",
    ),
    StrategyRunSpec(
        "cn_momentum_rotation", "top_n_picker",
        {"top_n": 8, "lock_days": 20, "max_replace": 1, "max_w": 0.12, "max_gross": 0.90},
        universe="all", strict=True, min_amount=MIN_AMOUNT, tier="warm",
    ),
    StrategyRunSpec(
        "pure_factor", "top_n_picker", dict(_TOP_N_STD),
        universe="all", strict=True, min_amount=MIN_AMOUNT, tier="warm",
    ),
    StrategyRunSpec(
        "dual_bollinger", "top_n_picker", dict(_TOP_N_BOLL),
        universe="all", strict=True, min_amount=MIN_AMOUNT, tier="warm",
    ),
    # ── COLD(布林日/周常冷补) ────────────────────────────────────
    StrategyRunSpec(
        "bollinger_reversion", "top_n_picker", dict(_TOP_N_BOLL),
        universe="universe_788", strict=False, min_amount=None, tier="cold",
    ),
    StrategyRunSpec(
        "bollinger_reversion_cross_section", "cap_and_rank", dict(_CS),
        universe="all", strict=True, min_amount=MIN_AMOUNT, tier="cold",
    ),
]

_CATALOG_BY_ID: dict[str, StrategyRunSpec] = {s.strategy_id: s for s in FULL_CYCLE_CATALOG}
_TIER_ORDER = ("hot", "warm", "cold")


def list_catalog() -> list[StrategyRunSpec]:
    """返回完整目录(按 hot→warm→cold 顺序)。"""
    return list(FULL_CYCLE_CATALOG)


def catalog_ids() -> list[str]:
    return [s.strategy_id for s in FULL_CYCLE_CATALOG]


def resolve_specs(strategy_ids: Iterable[str] | None) -> list[StrategyRunSpec]:
    """解析要跑的规格列表。

    strategy_ids 为空 / None → 全部; 否则只保留点名的 id(保持目录顺序)。
    未知 id 抛 ValueError。
    """
    if not strategy_ids:
        return list(FULL_CYCLE_CATALOG)
    wanted = []
    unknown = []
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
            f"未知 strategy_id: {unknown}; 可选: {catalog_ids()}"
        )
    if not wanted:
        return list(FULL_CYCLE_CATALOG)
    # 保持目录 tier 顺序
    order = {s.strategy_id: i for i, s in enumerate(FULL_CYCLE_CATALOG)}
    wanted_set = set(wanted)
    return sorted(
        (_CATALOG_BY_ID[sid] for sid in wanted_set),
        key=lambda s: order[s.strategy_id],
    )


def detect_data_end(explicit: str | date | None = None) -> str:
    """确定全周期 end。

    优先 explicit;否则取 QuoteSnapshot / EtfQuoteDaily 的 max(quote_date)。
    库空则退回今天。
    """
    if explicit is not None:
        if isinstance(explicit, date):
            return explicit.isoformat()
        return str(explicit)

    from sqlalchemy import func

    from stockfu.db import session_scope
    from stockfu.models import EtfQuoteDaily, QuoteSnapshot

    best: date | None = None
    with session_scope() as s:
        for model in (QuoteSnapshot, EtfQuoteDaily):
            d = s.exec(select(func.max(model.quote_date))).one()
            if d is not None and (best is None or d > best):
                best = d
    return (best or date.today()).isoformat()


def _load_universe_788() -> list[str]:
    path = Path(__file__).resolve().parents[2] / "data" / "backtest" / "universe-788.txt"
    if path.is_file():
        codes = [ln.strip() for ln in path.read_text(encoding="utf-8").split() if ln.strip()]
        if codes:
            return codes
    # 文件缺失时退回大盘池,避免硬失败
    from stockfu.services.universe import resolve_base_codes
    return resolve_base_codes("all")


def _resolve_codes(spec: StrategyRunSpec) -> tuple[list[str], Any]:
    """→ (codes, universe_rules|None)。"""
    from stockfu.db import session_scope
    from stockfu.models import EtfQuoteDaily
    from stockfu.services.universe import UniverseRules, resolve_base_codes

    if spec.universe == "etf":
        with session_scope() as s:
            codes = sorted({
                c for c in s.exec(select(EtfQuoteDaily.asset_code).distinct()).all() if c
            })
        return codes, None
    if spec.universe == "universe_788":
        return _load_universe_788(), None
    # all
    codes = resolve_base_codes("all")
    rules = None
    if spec.min_amount is not None:
        rules = UniverseRules(min_amount_ma20=spec.min_amount)
    return codes, rules


def _snapshot_app_config() -> dict[str, str | None]:
    from stockfu.db import get_app_config
    keys = ("active_strategy_id", "active_rebalancer_id", "rebalancer_params")
    return {k: get_app_config(k) for k in keys}


def _restore_app_config(snap: dict[str, str | None]) -> None:
    from stockfu.db import set_app_config
    for k, v in snap.items():
        if v is not None:
            set_app_config(k, v)


def _apply_spec(spec: StrategyRunSpec) -> None:
    from stockfu.db import set_app_config
    set_app_config("active_strategy_id", spec.strategy_id)
    set_app_config("active_rebalancer_id", spec.rebalancer_id)
    set_app_config("rebalancer_params", json.dumps(spec.rebalancer_params or {}))


def run_one(
    spec: StrategyRunSpec,
    start: str,
    end: str,
    *,
    cash: float = 1_000_000.0,
    run_id: str | None = None,
) -> dict[str, Any]:
    """跑单策略全周期;返回摘要 dict(含 metrics / error)。"""
    from stockfu.backtest.scheduler import run as scheduler_run

    codes, rules = _resolve_codes(spec)
    if not codes:
        return {
            "strategy_id": spec.strategy_id,
            "ok": False,
            "error": f"空宇宙 universe={spec.universe}",
            "tier": spec.tier,
        }

    _apply_spec(spec)
    rid = run_id or f"upd-{spec.strategy_id}-{end}"
    t0 = time.time()
    try:
        r = scheduler_run(
            codes, start, end,
            initial_cash=cash,
            run_id=rid,
            strict=spec.strict,
            universe_rules=rules,
        )
    except Exception as e:
        return {
            "strategy_id": spec.strategy_id,
            "ok": False,
            "error": str(e),
            "traceback": traceback.format_exc(limit=8),
            "tier": spec.tier,
            "elapsed_sec": round(time.time() - t0, 1),
        }

    m = r.get("metrics") or {}
    return {
        "strategy_id": spec.strategy_id,
        "strategy_name": r.get("strategy_name"),
        "ok": True,
        "tier": spec.tier,
        "rebalancer_id": spec.rebalancer_id,
        "universe": spec.universe,
        "strict": spec.strict,
        "n_codes": len(codes),
        "start": start,
        "end": end,
        "run_id": r.get("run_id"),
        "total_return": m.get("total_return"),
        "annualized": m.get("annualized"),
        "max_drawdown": m.get("max_drawdown"),
        "sharpe": m.get("sharpe"),
        "excess": m.get("excess"),
        "benchmark_return": m.get("benchmark_return"),
        "avg_gross_leverage": m.get("avg_gross_leverage"),
        "annual_turnover": m.get("annual_turnover"),
        "trade_count": m.get("trade_count"),
        "final_equity": m.get("final_equity"),
        # 对比指标(引擎原生产出,见 engine _metrics/Stage B);1:1 映射 PROJECT_STATE §0.6 表。
        "max_drawdown_recovery_days": m.get("max_drawdown_recovery_days"),
        "max_drawdown_recovered": m.get("max_drawdown_recovered"),
        "distinct_stocks_bought": m.get("distinct_stocks_bought"),
        "stop_loss_count": m.get("stop_loss_count"),
        "stop_loss_realized_loss": m.get("stop_loss_realized_loss"),
        "underwater_pct_ge20": m.get("underwater_pct_ge20"),
        "elapsed_sec": round(time.time() - t0, 1),
        "saved_to": r.get("saved_to"),
    }


def update_backtests(
    strategy_ids: list[str] | None = None,
    *,
    start: str | None = None,
    end: str | None = None,
    cash: float = 1_000_000.0,
    dry_run: bool = False,
    save_summary: bool = True,
) -> dict[str, Any]:
    """批跑全周期更新。

    Args:
        strategy_ids: 要更新的策略;None/[] = 目录全部
        start: 默认 DEFAULT_START
        end: 默认 detect_data_end()
        dry_run: 只打印计划不跑
        save_summary: 写 data/backtest/update_summary-*.json

    Returns:
        {start, end, planned, results, summary_path?}
    """
    from stockfu.ai.operators.registry import discover_and_register
    from stockfu.ai.operators.seed import seed_operators_and_strategies
    from stockfu.db import init_db

    # 先解析目录(不碰 DB),未知 id 尽早失败
    specs = resolve_specs(strategy_ids)
    start_s = start or DEFAULT_START

    if dry_run:
        # dry-run 仍探测 end(一次轻量 SQL);不 seed、不跑回测
        from stockfu.db import init_db
        init_db()
        end_s = detect_data_end(end)
    else:
        init_db()
        discover_and_register()
        seed_operators_and_strategies()
        end_s = detect_data_end(end)

    plan = [
        {
            "strategy_id": s.strategy_id,
            "rebalancer_id": s.rebalancer_id,
            "rebalancer_params": s.rebalancer_params,
            "universe": s.universe,
            "strict": s.strict,
            "min_amount": s.min_amount,
            "tier": s.tier,
        }
        for s in specs
    ]

    print(
        f"=== 全周期回测更新  {start_s} → {end_s}  "
        f"共 {len(specs)} 策略"
        f"{'  [dry-run]' if dry_run else ''} ===",
        flush=True,
    )
    for i, s in enumerate(specs, 1):
        print(
            f"  [{i}/{len(specs)}] {s.tier:4} {s.strategy_id:40} "
            f"reb={s.rebalancer_id} uni={s.universe} strict={s.strict}",
            flush=True,
        )

    if dry_run:
        return {
            "start": start_s,
            "end": end_s,
            "dry_run": True,
            "planned": plan,
            "results": [],
        }

    snap = _snapshot_app_config()
    results: list[dict[str, Any]] = []
    try:
        for i, spec in enumerate(specs, 1):
            print(
                f"\n[{i}/{len(specs)}] 跑 {spec.strategy_id} "
                f"({spec.tier}, {spec.rebalancer_id}) …",
                flush=True,
            )
            row = run_one(spec, start_s, end_s, cash=cash)
            results.append(row)
            if row.get("ok"):
                print(
                    f"  ✓ {row.get('elapsed_sec')}s | "
                    f"收益 {row.get('total_return')}% | "
                    f"年化 {row.get('annualized')}% | "
                    f"回撤 {row.get('max_drawdown')}% | "
                    f"夏普 {row.get('sharpe')} | "
                    f"超额 {row.get('excess')}% | "
                    f"run_id={row.get('run_id')}",
                    flush=True,
                )
            else:
                print(f"  ✗ FAIL: {row.get('error')}", flush=True)
    finally:
        _restore_app_config(snap)

    ok_n = sum(1 for r in results if r.get("ok"))
    fail_n = len(results) - ok_n
    print(f"\n=== 完成: 成功 {ok_n} / 失败 {fail_n} / 共 {len(results)} ===", flush=True)

    out: dict[str, Any] = {
        "schema_version": 2,
        "kind": "full_cycle_update",
        "start": start_s,
        "end": end_s,
        "dry_run": False,
        "planned": plan,
        "results": results,
        "ok": ok_n,
        "fail": fail_n,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
    }

    if save_summary:
        from stockfu.backtest.scheduler import _data_dir
        d = _data_dir()
        Path(d).mkdir(parents=True, exist_ok=True)
        name = f"update_summary-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        path = str(Path(d) / name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2, default=str)
        out["summary_path"] = path
        print(f"摘要已写: {path}", flush=True)

    return out


def print_catalog() -> None:
    """打印可更新策略目录。"""
    print(f"{'tier':4}  {'strategy_id':40}  {'rebalancer':16}  universe  strict")
    print("-" * 94)
    for s in FULL_CYCLE_CATALOG:
        print(
            f"{s.tier:4}  {s.strategy_id:40}  {s.rebalancer_id:16}  "
            f"{s.universe:10}  {s.strict}"
        )
    print(f"\n共 {len(FULL_CYCLE_CATALOG)} 条 | 默认 start={DEFAULT_START} | "
          f"end=库内行情末日")
