#!/usr/bin/env python3
"""V1 策略归档生成与删除门禁校验（docs/SPECS/factor-strategy-score-v2.md §17）。

用法：
    python scripts/archive_v1_strategies.py generate   # 生成 docs/legacy/strategy-v1/ 全部产物
    python scripts/archive_v1_strategies.py verify     # 重新跑门禁 1-4（generate 结束时自动执行）

产物（§17.2 七类）：
    catalog.yaml           52 源文件 + 全部 variants 展开后的有效配置（含隐式默认展开与来源标注）
    catalog.md             中文汇总（目的/假设/结论/迁移/复现命令）
    strategy-source/*.yaml 原 52 个 YAML 只读文本副本（保留注释，不在运行时路径）
    runtime-bindings.yaml  策略曾使用的 rebalancer、股票池、调度入口、默认 app_config、费用口径
    result-index.csv       历史回测 artifact 索引（路径/起止日/指标/checksum）
    migration-map.yaml     old strategy_id -> archive_only / V2 alpha/policy/risk 组合
    checksums.sha256       上述全部产物 + 原 YAML 的 SHA-256

校验（§17.3 门禁 1-4，其余门禁需人工/后续阶段）：
    1. 52/52 基础文件收录；seed 基础选择 29、_RETAINED_STRATEGY_IDS 31；
       展开 id 与 seed._expand_variants 一致。
    2. 每个源文件 checksum 与归档副本一致。
    3. 每个源策略能由 catalog.yaml 重新渲染；31 个 retained 运行 id 渲染结果
       与当前 seed 等价（即 _expand_variants 输出的 vtext 编译结果）。
    4. 渲染结果逐字段等于 compile_strategy 加载后的配置（含隐式默认值展开）。
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from stockfu.ai.operators.runner import compile_strategy
from stockfu.ai.operators.seed import (
    _RETAINED_STRATEGY_IDS,
    _STRATEGIES,
    _STRATEGIES_DIR,
    _expand_variants,
    _load_strategy_yaml,
)

OUT = ROOT / "docs" / "legacy" / "strategy-v1"
SOURCE_DIR = OUT / "strategy-source"

# ---------------------------------------------------------------------------
# §18.3 迁移备注 -> 结构化 migration-map（人工整理自设计文档，勿自动改）
# disposition: keep(继续作为 V1 运行候选) | archive_only(已证伪/不复用) |
#              rebuild(V2 需重建) | research(单因子研究) | v2_candidate(V2 已有对应)
# ---------------------------------------------------------------------------
MIGRATION_MAP: dict[str, dict] = {
    "amplitude": {"disposition": "research", "v2_target": "amplitude", "note": "单因子研究，归档后用 V2 amplitude 重建"},
    "anti_lottery_defensive": {"disposition": "rebuild", "v2_target": None, "note": "value 拆分后重新研究"},
    "bias_reversal": {"disposition": "research", "v2_target": None, "note": "单因子研究"},
    "bollinger_reversion": {"disposition": "rebuild", "v2_target": None, "note": "意图 top_n，V2 重组"},
    "bollinger_reversion_cross_section": {"disposition": "rebuild", "v2_target": None, "note": "意图 cap_and_rank"},
    "cn_momentum_cross_section": {"disposition": "keep", "v2_target": "momentum_cross_section", "note": ""},
    "cn_momentum_rotation": {"disposition": "archive_only", "v2_target": None, "note": "通用策略已证伪（年化 4.34%/Sharpe .30/回撤 31.62%）"},
    "cross_section_factor": {"disposition": "v2_candidate", "v2_target": "reversal_value", "note": ""},
    "dividend_cross_section": {"disposition": "v2_candidate", "v2_target": "dividend_quality_value", "note": "variants sl30/sl30w10/sl30w20；H1 只保留一个 alpha，止盈/刹车并入 policy/risk（§18.4）"},
    "dividend_cross_section_atr_lagged_take_profit": {"disposition": "v2_candidate", "v2_target": "dividend_quality_value", "note": "RHatrLag 并入 risk policy"},
    "dividend_cross_section_atr_take_profit": {"disposition": "v2_candidate", "v2_target": "dividend_quality_value", "note": "RHatr 并入 risk policy"},
    "dividend_cross_section_partial_brake_take_profit": {"disposition": "v2_candidate", "v2_target": "dividend_quality_value", "note": "RHbrake 并入 risk policy"},
    "dividend_cross_section_partial_drawdown_add_gated_take_profit": {"disposition": "v2_candidate", "v2_target": "dividend_quality_value", "note": "RHadd 简单版（max_gross=1.00）"},
    "dividend_cross_section_partial_exposure_add_gated_hold_take_profit": {"disposition": "v2_candidate", "v2_target": "dividend_quality_value", "note": "H2 + RHaddHold"},
    "dividend_cross_section_partial_exposure_add_gated_take_profit": {"disposition": "v2_candidate", "v2_target": "dividend_quality_value", "note": "RHadd 分级版"},
    "dividend_cross_section_partial_exposure_brake_hold_take_profit": {"disposition": "v2_candidate", "v2_target": "dividend_quality_value", "note": "H2 + RHhold"},
    "dividend_cross_section_partial_exposure_brake_regime_trend_take_profit": {"disposition": "v2_candidate", "v2_target": "dividend_quality_value", "note": "RHtrend 并入 risk policy"},
    "dividend_cross_section_partial_exposure_brake_regime_trendvol_take_profit": {"disposition": "v2_candidate", "v2_target": "dividend_quality_value", "note": "RHtrendvol"},
    "dividend_cross_section_partial_exposure_brake_regime_vol_take_profit": {"disposition": "v2_candidate", "v2_target": "dividend_quality_value", "note": "RHvol"},
    "dividend_cross_section_partial_exposure_brake_take_profit": {"disposition": "v2_candidate", "v2_target": "dividend_quality_value", "note": "RHdeep；variant deep/rec125"},
    "dividend_cross_section_partial_gentle_brake_take_profit": {"disposition": "v2_candidate", "v2_target": "dividend_quality_value", "note": "RHgentle"},
    "dividend_cross_section_partial_selective_brake_take_profit": {"disposition": "v2_candidate", "v2_target": "dividend_quality_value", "note": "RHselect"},
    "dividend_cross_section_partial_take_profit": {"disposition": "v2_candidate", "v2_target": "dividend_quality_value", "note": "RHpartial"},
    "dividend_cross_section_take_profit": {"disposition": "v2_candidate", "v2_target": "dividend_quality_value", "note": "RHfull"},
    "dividend_low_vol": {"disposition": "v2_candidate", "v2_target": "dividend_quality_value", "note": "意图 top_n lock20"},
    "donchian_breakout_cross_section": {"disposition": "keep", "v2_target": None, "note": "stop=.18/gross=1/brake=0；ATR20 止盈两档"},
    "dual_bollinger": {"disposition": "rebuild", "v2_target": None, "note": "均值回归与动量方向需重新审视"},
    "etf_momentum_cross_section": {"disposition": "keep", "v2_target": "etf_momentum_cross_section", "note": "ETF market_scope 单列"},
    "etf_momentum_rotation": {"disposition": "keep", "v2_target": None, "note": "意图 top5 lock20"},
    "fifty_two_week_high_cross_section": {"disposition": "archive_only", "v2_target": None, "note": "旧注释称满仓配置已证伪，先 archive_only"},
    "graham_defensive_value": {"disposition": "archive_only", "v2_target": None, "note": "存在股息重复，旧 alpha 不复用"},
    "illiquidity_value": {"disposition": "rebuild", "v2_target": None, "note": "需加容量硬门禁后重建"},
    "intraday_return": {"disposition": "research", "v2_target": None, "note": "单因子研究"},
    "limit_up_count": {"disposition": "research", "v2_target": None, "note": "V2 作为单边惩罚研究"},
    "low_beta_dividend": {"disposition": "rebuild", "v2_target": "defensive_family", "note": "value 拆分后重建 defensive 家族"},
    "low_downside_vol": {"disposition": "research", "v2_target": None, "note": "单因子研究"},
    "low_skewness": {"disposition": "research", "v2_target": None, "note": "因子方向需实证（downside_skewness 已从注册表下线）"},
    "low_turnover_reversal": {"disposition": "v2_candidate", "v2_target": "liquidity_size", "note": ""},
    "macd_cross": {"disposition": "rebuild", "v2_target": None, "note": "离散因子删除后按日/周连续强度重建"},
    "momentum_breakout": {"disposition": "keep", "v2_target": None, "note": "实质为动量追涨，不是布林回归"},
    "momentum_breakout_cross_section": {"disposition": "keep", "v2_target": None, "note": ""},
    "near_52w_low": {"disposition": "research", "v2_target": None, "note": "单因子研究"},
    "overnight_reversal": {"disposition": "research", "v2_target": None, "note": "单因子研究"},
    "pure_factor": {"disposition": "archive_only", "v2_target": None, "note": "因子方向互相冲突"},
    "residual_reversal": {"disposition": "rebuild", "v2_target": None, "note": "修复历史 beta 口径后重建"},
    "reversal_cross_section": {"disposition": "keep", "v2_target": None, "note": ""},
    "reversal_strategy": {"disposition": "keep", "v2_target": None, "note": "意图 top8 lock20"},
    "rsi_reversal": {"disposition": "research", "v2_target": None, "note": "与 mean_reversion 合并后研究"},
    "small_cap_low_turnover": {"disposition": "rebuild", "v2_target": None, "note": "可靠市值与容量门禁就绪后重建"},
    "smart_beta_multi_factor": {"disposition": "archive_only", "v2_target": None, "note": "value/graham 重复"},
    "ts_momentum_trend": {"disposition": "v2_candidate", "v2_target": "momentum_trend", "note": "另 target_vol=.15/vol_window=63/vol_floor=.30"},
    "volume_drought": {"disposition": "research", "v2_target": None, "note": "单因子研究"},
}

# §18.1 公共配置缩写展开（人工整理自设计文档，用于 catalog.md 可读摘要）
_ABBREV = {
    "W12": "weighted_sum thresholds strong_buy=12/buy=4/hold=-4/sell=-12",
    "W8": "weighted_sum thresholds 8/3/-3/-8",
    "W10": "weighted_sum thresholds 10/4/-4/-10",
    "D1": "buy_cool_down=1/sell_cooldown=1/max_target_step=1.0/risk_confirm=1/min_trade_weight=0.01/conf_gate=0.0",
    "Drot": "buy=5d/sell=3d/max_target_step=0.3/risk_confirm=1/min_trade_weight=0.01/conf_gate=0.3",
    "Dmacd": "buy=3d/sell=0d/max_target_step=1.0/risk_confirm=1/min_trade_weight=0.01/conf_gate=0",
    "Dhold": "buy=30d/sell=30d/max_target_step=1.0/risk_confirm=1/min_trade_weight=0.01/conf_gate=0",
    "P20": "position continuous max_w=0.20 dead=3 score_full=8",
    "P05": "position continuous max_w=0.05 dead=3 score_full=8",
    "Prot12": "position continuous max_w=0.12 dead=3 score_full=8",
    "Prot10": "position continuous max_w=0.10 dead=3 score_full=8(隐式)",
    "R0": "risk stop_loss=0/portfolio_brake=0/max_gross=1.0",
    "Rimplicit": "risk 段未写，运行时采用引擎默认（stop_loss=0.08/brake=0.10/scale=0.50/max_gross=0.90）",
    "H1": "dividend_yield[1.0; high_yield=5.0,price_basis=raw,yield_cap=20.0] + low_volatility[0.8;20,3y] + value[0.6;5y]，W12",
    "H2": "H1 三因子权重均 1.0；sell_weights dividend_yield=2/low_volatility=1/value=2",
}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_head() -> str:
    try:
        import subprocess
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                             capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except Exception:
        return "unknown"


def _git_dirty() -> bool:
    try:
        import subprocess
        out = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                             capture_output=True, text=True, check=True)
        return bool(out.stdout.strip())
    except Exception:
        return True


def _collect_entries() -> list[dict]:
    """遍历 strategies/ 下全部 52 个基础文件（含未入选 seed 的旧策略）并展开 variants。

    顺序：_STRATEGIES 保持 seed 顺序优先，其余按字母序补尾。返回 catalog entry 列表。
    """
    base_ids = list(_STRATEGIES)
    base_ids += sorted(
        p.stem for p in _STRATEGIES_DIR.glob("*.yaml") if p.stem not in _STRATEGIES)
    entries: list[dict] = []
    for base_id in base_ids:
        name, text = _load_strategy_yaml(base_id)
        src_path = _STRATEGIES_DIR / f"{base_id}.yaml"
        src_sha = _sha256(src_path)
        rows = _expand_variants(base_id, text)
        for vsid, vname, vtext, derived in rows:
            cs = compile_strategy(vtext, strategy_id=vsid)
            entries.append({
                "strategy_id": vsid,
                "base_id": base_id,
                "name": vname,
                "source_file": f"{base_id}.yaml",
                "source_sha256": src_sha,
                "derived": derived,
                "retained": vsid in _RETAINED_STRATEGY_IDS,
                "yaml_text": vtext,
                "compiled": {
                    "operators": cs.operators,
                    "aggregate": cs.aggregate,
                    "position": cs.position,
                    "debounce": cs.debounce,
                    "risk": cs.risk,
                },
                # 最终有效参数：debounce_params() 把 position/debounce/risk 段与
                # 隐式默认合并展开（来源见 runner.py CompiledStrategy.debounce_params）
                "effective": dataclasses.asdict(cs.debounce_params),
            })
    return entries


def _render_catalog_yaml(entries: list[dict]) -> dict:
    """catalog.yaml 主体：展开策略的完整有效配置（§17.2 字段清单）。"""
    out = {
        "schema_version": 1,
        "generated_at": dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds"),
        "git": {"commit": _git_head(), "dirty": _git_dirty()},
        "source_counts": {
            "base_files_archived": 52,
            "base_files_in_seed": len(_STRATEGIES),
            "retained_ids": len(_RETAINED_STRATEGY_IDS),
            "expanded_entries": len(entries),
        },
        "defaults_notes": {
            "runtime_defaults_source": "stockfu/ai/operators/runner.py CompiledStrategy.debounce_params()",
            "engine_defaults_source": "stockfu/backtest/engine.py 顶层常量",
            "rebalancer_source": "app_config active_rebalancer_id/rebalancer_params（seed 首次写入）",
        },
        "strategies": [],
    }
    for e in entries:
        out["strategies"].append({
            "strategy_id": e["strategy_id"],
            "base_id": e["base_id"],
            "name": e["name"],
            "source_file": e["source_file"],
            "source_sha256": e["source_sha256"],
            "derived": e["derived"],
            "retained": e["retained"],
            "compiled": e["compiled"],
            "effective": e["effective"],
        })
    return out


def _write_runtime_bindings(entries: list[dict]) -> None:
    """runtime-bindings.yaml：V1 回测/生产实际使用的运行绑定（代码事实）。

    历史上只有少数策略进入 full-cycle catalog；其余策略的外部
    rebalancer/app_config 绑定无法从当前代码恢复时，必须显式写
    ``unrecoverable``，不能用当前默认值冒充历史事实。
    """
    from stockfu.backtest import engine as eng
    from stockfu.backtest.full_cycle_update import FULL_CYCLE_CATALOG

    full_cycle = {s.strategy_id: s for s in FULL_CYCLE_CATALOG}
    strategy_bindings = {}
    for e in entries:
        spec = full_cycle.get(e["strategy_id"])
        if spec is None:
            # base strategy 与 catalog 中的 variant 可能共享一套明确 binding。
            spec = full_cycle.get(e["base_id"])
        if spec is None:
            rebalancer = {
                "id": "unrecoverable",
                "params": "unrecoverable",
                "source": "当前代码无法恢复该策略历史运行时外部绑定",
            }
            universe = {
                "id": "unrecoverable",
                "min_amount": "unrecoverable",
                "source": "当前代码无法恢复该策略历史运行时股票池覆盖",
            }
            binding_status = "unrecoverable"
        else:
            rebalancer = {
                "id": spec.rebalancer_id,
                "params": dict(spec.rebalancer_params),
                "source": "stockfu/backtest/full_cycle_update.py FULL_CYCLE_CATALOG",
            }
            universe = {
                "id": spec.universe,
                "min_amount": spec.min_amount,
                "source": "stockfu/backtest/full_cycle_update.py StrategyRunSpec",
            }
            binding_status = "known"
        strategy_bindings[e["strategy_id"]] = {
            "base_id": e["base_id"],
            "variant_key": e["strategy_id"][len(e["base_id"]) + 1:]
            if e["derived"] else None,
            "disposition": MIGRATION_MAP[e["base_id"]]["disposition"],
            "status": binding_status,
            "rebalancer": rebalancer,
            "universe": universe,
            "cost_model": "engine_constants",
            "entrypoints": {
                "backtest": "python main.py --backtest <strategy_id>",
                "recommend": "python main.py --recommend --strategies <strategy_id>",
                "signal_scan": "stockfu/services/signal_scan.py",
            },
        }

    bindings = {
        "schema_version": 1,
        "rebalancer": {
            "active_rebalancer_id_default": "pass_through",
            "rebalancer_params_default": {"max_gross": 0.90, "max_w": 0.10},
            "max_gross_precedence": "yaml risk.max_gross（debounce.max_gross）> app_config rebalancer_params > engine DEFAULT_MAX_GROSS",
            "note": "回测 CLI 无 --rebalancer 参数；engine.run 内部 get_active_rebalancer()/get_rebalancer_params() 读 app_config（stockfu/backtest/engine.py:1548）",
        },
        "universe": {
            "default": "historical_indices（历史沪深300 宇宙）",
            "source": "stockfu/services/index_universe.py HISTORICAL_INDEX_CODES / HISTORICAL_UNIVERSE_ID",
            "override": "--codes 指定代码列表；--min-amount 过滤（main.py --backtest 分支）",
        },
        "engine_constants": {
            "initial_cash": eng.INITIAL_CASH,
            "commission_rate": eng.COMMISSION_RATE,
            "min_commission": eng.MIN_COMMISSION,
            "stamp_duty_rate": eng.STAMP_DUTY_RATE,
            "stamp_duty_rate_old": eng.STAMP_DUTY_RATE_OLD,
            "stamp_duty_cutoff": str(eng.STAMP_DUTY_CUTOFF),
            "transfer_fee_rate": eng.TRANSFER_FEE_RATE,
            "slippage": "无显式滑点模型（以收盘价成交，见 engine 执行层）",
            "default_max_gross": eng.DEFAULT_MAX_GROSS,
            "default_stop_loss": eng.DEFAULT_STOP_LOSS,
            "default_portfolio_brake": eng.DEFAULT_PORTFOLIO_BRAKE,
            "default_portfolio_brake_scale": eng.DEFAULT_PORTFOLIO_BRAKE_SCALE,
        },
        "entrypoints": {
            "backtest": "python main.py --backtest <strategy_id> [--start --end --cash --codes --min-amount --save]",
            "recommend": "python main.py --recommend --strategies <ids> [--as-of --cash]",
            "signal_mail": "python main.py --signal-mail / scheduler jobs.run_signal_pipeline",
            "evaluate": "python main.py --evaluate（stockfu/services/evaluator.py 自选股多策略评价矩阵）",
            "backfill_dividend": "python main.py --backfill-dividend（dividend_yield 算子数据依赖）",
        },
        "data_dependencies": {
            "kline": "主库 data/stockfu.db 行情/分红/宇宙表",
            "operator_cache": "data/operator_cache.db operator_result（可再生的单算子缓存，operator_id+params+fingerprint 定位）",
            "dividend_events": "dividend_event 表（--backfill-dividend 灌入）",
        },
        "strategies": strategy_bindings,
    }
    (OUT / "runtime-bindings.yaml").write_text(
        yaml.safe_dump(bindings, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _write_result_index(entries: list[dict]) -> None:
    """result-index.csv：扫描 data/backtest/ 既有 artifact（§17.3 门禁 6 素材）。"""
    bt = ROOT / "data" / "backtest"
    rows = []
    metas = sorted(bt.glob("run-*.meta.json"))
    for mp in metas:
        m = json.loads(mp.read_text(encoding="utf-8"))
        data_file = mp.with_name(mp.name.replace(".meta.json", ".json.gz"))
        metrics = m.get("metrics") or {}
        row = {
            "run_id": m.get("run_id", mp.stem),
            "strategy_id": m.get("strategy_id", ""),
            "strategy_name": m.get("strategy_name", ""),
            "start": m.get("start", ""),
            "end": m.get("end", ""),
            "days": m.get("days", ""),
            "total_return": metrics.get("total_return", ""),
            "annualized": metrics.get("annualized", ""),
            "max_drawdown": metrics.get("max_drawdown", ""),
            "sharpe": metrics.get("sharpe", ""),
            "trade_count": metrics.get("trade_count", ""),
            "final_equity": metrics.get("final_equity", ""),
            "benchmark_return": metrics.get("benchmark_return", ""),
            "artifact": str(data_file.relative_to(ROOT)),
            "data_size": data_file.stat().st_size if data_file.exists() else "not_available",
            "sha256": _sha256(data_file) if data_file.exists() else "not_available",
            "verdict": "",  # 是否证伪：按 docs/BACKTEST.md 章节人工标注
        }
        rows.append(row)

    # 门禁 6 要求缺失结果显式可见；为所有展开 old id 补 not_available 行，
    # 这样“没有跑过”不会与“归档脚本漏扫”混淆。
    indexed_ids = {str(row.get("strategy_id") or "") for row in rows}
    for e in entries:
        sid = e["strategy_id"]
        if sid in indexed_ids:
            continue
        rows.append({
            "run_id": f"not_available:{sid}",
            "strategy_id": sid,
            "strategy_name": e["name"],
            "start": "",
            "end": "",
            "days": "",
            "total_return": "",
            "annualized": "",
            "max_drawdown": "",
            "sharpe": "",
            "trade_count": "",
            "final_equity": "",
            "benchmark_return": "",
            "artifact": "not_available",
            "data_size": "not_available",
            "sha256": "not_available",
            "verdict": "not_available",
        })
    cols = [
        "run_id", "strategy_id", "strategy_name", "start", "end", "days",
        "total_return", "annualized", "max_drawdown", "sharpe", "trade_count",
        "final_equity", "benchmark_return", "artifact", "data_size", "sha256",
        "verdict",
    ]
    with (OUT / "result-index.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def _write_strategy_sources() -> None:
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    for src in sorted(_STRATEGIES_DIR.glob("*.yaml")):
        (SOURCE_DIR / src.name).write_bytes(src.read_bytes())


def _write_checksums() -> None:
    lines = []
    for p in sorted(OUT.rglob("*")):
        if p.is_file() and p.name != "checksums.sha256":
            lines.append(f"{_sha256(p)}  {p.relative_to(OUT)}")
    for src in sorted(_STRATEGIES_DIR.glob("*.yaml")):
        lines.append(f"{_sha256(src)}  source/{src.name}  (原 strategies/{src.name})")
    (OUT / "checksums.sha256").write_text("\n".join(sorted(lines)) + "\n", encoding="utf-8")


def _write_catalog_md(entries: list[dict]) -> None:
    lines = [
        "# V1 策略归档目录（strategy-v1）",
        "",
        "> 依据 docs/SPECS/factor-strategy-score-v2.md §17。V1 的 52 份 YAML 不作为 V2 活跃配置复用；",
        "> 本目录把配置与结论冻结为可校验产物。删除门禁（§17.3）通过前，V1 代码与缓存不得删除。",
        "",
        f"- 生成时间：{dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec='seconds')}",
        f"- git commit：`{_git_head()}`（dirty={_git_dirty()}）",
        f"- 源文件：52 个基础 YAML（`stockfu/ai/strategies/` 全部收录），seed 选择 {len(_STRATEGIES)} 个，展开后 {len(entries)} 条配置",
        f"- 保留运行 id：{len(_RETAINED_STRATEGY_IDS)} 个（seed._RETAINED_STRATEGY_IDS）",
        "",
        "## 文件说明",
        "",
        "| 文件 | 说明 |",
        "|---|---|",
        "| catalog.yaml | 全部展开策略的完整有效配置（含隐式默认展开，机器可读主表） |",
        "| catalog.md | 本文件：中文目的、缩写展开、已知结论、迁移方向 |",
        "| strategy-source/ | 原 52 个 YAML 只读副本（保留注释） |",
        "| runtime-bindings.yaml | rebalancer/宇宙/调度入口/费用口径等运行绑定 |",
        "| result-index.csv | 历史回测 artifact 索引（路径/指标/checksum） |",
        "| migration-map.yaml | old id -> archive_only / V2 组合映射 |",
        "| checksums.sha256 | 全部产物 + 原 YAML 的 SHA-256 |",
        "",
        "## §18.1 公共配置缩写展开",
        "",
    ]
    for k in ("W12", "W8", "W10", "D1", "Drot", "Dmacd", "Dhold",
              "P20", "P05", "Prot12", "Prot10", "R0", "Rimplicit", "H1", "H2"):
        lines.append(f"- **{k}**：{_ABBREV[k]}")
    lines += [
        "",
        "## 迁移总览（migration-map.yaml 摘要）",
        "",
        "| 处置 | 策略数 | 策略 |",
        "|---|---|---|",
    ]
    by_disp: dict[str, list[str]] = {}
    for e in entries:
        if e["derived"]:
            continue
        disp = MIGRATION_MAP[e["base_id"]]["disposition"]
        by_disp.setdefault(disp, []).append(e["base_id"])
    for disp in ("keep", "v2_candidate", "rebuild", "research", "archive_only"):
        ids = by_disp.get(disp, [])
        if ids:
            lines.append(f"| {disp} | {len(ids)} | {', '.join(ids)} |")
    lines += [
        "",
        "## 已知结论与复现",
        "",
        "- 通用策略已证伪（archive_only）：cn_momentum_rotation、fifty_two_week_high_cross_section、pure_factor、smart_beta_multi_factor、graham_defensive_value",
        "- 历史回测结论见 docs/BACKTEST.md（§0.6.x 归档章节）与 result-index.csv",
        "- 复现：`git checkout {_git_head()} && python main.py --backtest <strategy_id> --start --end --cash`（数据快照见 data/backtest/ 产物）",
        "",
    ]
    (OUT / "catalog.md").write_text("\n".join(lines), encoding="utf-8")


def _expanded_migration_map(entries: list[dict]) -> dict[str, dict]:
    """把 base 处置决定展开到每个 old strategy id（含 variants）。"""
    out: dict[str, dict] = {}
    for e in entries:
        base = dict(MIGRATION_MAP[e["base_id"]])
        base["base_id"] = e["base_id"]
        base["source_file"] = e["source_file"]
        if e["derived"]:
            base["variant_key"] = e["strategy_id"][len(e["base_id"]) + 1:]
            base["note"] = (
                f"继承 base 策略 {e['base_id']} 的处置；"
                f"{base.get('note') or '无额外备注'}"
            )
        else:
            base["variant_key"] = None
        out[e["strategy_id"]] = base
    return out


def _contains_unknown(value) -> bool:
    if isinstance(value, dict):
        return any(_contains_unknown(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_unknown(v) for v in value)
    return isinstance(value, str) and value.strip().lower() == "unknown"


def _verify(entries: list[dict]) -> int:
    """门禁 1-8（§17.3）。返回失败数，0 = 通过。"""
    fails: list[str] = []

    # ---- 门禁 1：52/52 收录 + 29 基础 + 31 retained + 展开 id 一致 ----
    src_files = sorted(_STRATEGIES_DIR.glob("*.yaml"))
    if len(src_files) != 52:
        fails.append(f"门禁1: strategies/*.yaml 数量={len(src_files)}，期望 52")
    if len(_STRATEGIES) != 29:
        fails.append(f"门禁1: seed._STRATEGIES={len(_STRATEGIES)}，期望 29")
    if len(_RETAINED_STRATEGY_IDS) != 31:
        fails.append(f"门禁1: _RETAINED_STRATEGY_IDS={len(_RETAINED_STRATEGY_IDS)}，期望 31")
    catalog_ids = {e["strategy_id"] for e in entries}
    if len(catalog_ids) != len(entries):
        fails.append("门禁1: catalog 展开 id 重复")
    retained_in_catalog = {e["strategy_id"] for e in entries if e["retained"]}
    if retained_in_catalog != set(_RETAINED_STRATEGY_IDS):
        fails.append(f"门禁1: retained 集合不一致: 多 {retained_in_catalog - set(_RETAINED_STRATEGY_IDS)} 缺 {set(_RETAINED_STRATEGY_IDS) - retained_in_catalog}")
    for base_id in _STRATEGIES:
        _, text = _load_strategy_yaml(base_id)
        expect = {sid for sid, *_ in _expand_variants(base_id, text)}
        got = {e["strategy_id"] for e in entries if e["base_id"] == base_id}
        if expect != got:
            fails.append(f"门禁1: {base_id} 展开 id 不一致: 多 {got - expect} 缺 {expect - got}")
    # 未入选 seed 的 23 个旧策略文件也必须收录且展开一致
    for src in src_files:
        if src.stem in _STRATEGIES:
            continue
        expect = {sid for sid, *_ in _expand_variants(src.stem, src.read_text(encoding="utf-8"))}
        got = {e["strategy_id"] for e in entries if e["base_id"] == src.stem}
        if expect != got:
            fails.append(f"门禁1: {src.stem} 展开 id 不一致: 多 {got - expect} 缺 {expect - got}")
    catalog_bases = {e["base_id"] for e in entries}
    src_bases = {s.stem for s in src_files}
    if catalog_bases != src_bases:
        fails.append(f"门禁1: catalog 基础文件集合不一致: 多 {catalog_bases - src_bases} 缺 {src_bases - catalog_bases}")

    # ---- 门禁 2：源文件 checksum 与归档副本一致 ----
    for src in src_files:
        copy = SOURCE_DIR / src.name
        if not copy.exists():
            fails.append(f"门禁2: 缺归档副本 {src.name}")
        elif _sha256(src) != _sha256(copy):
            fails.append(f"门禁2: 归档副本与源不一致 {src.name}")

    # ---- 门禁 3/4：catalog 渲染 == compile_strategy（含隐式默认），retained 与 seed 等价 ----
    for e in entries:
        cs = compile_strategy(e["yaml_text"], strategy_id=e["strategy_id"])
        compiled = {
            "operators": cs.operators,
            "aggregate": cs.aggregate,
            "position": cs.position,
            "debounce": cs.debounce,
            "risk": cs.risk,
        }
        if compiled != e["compiled"]:
            fails.append(f"门禁3/4: {e['strategy_id']} 渲染后 compiled 不一致")
        effective = dataclasses.asdict(cs.debounce_params)
        if effective != e["effective"]:
            fails.append(f"门禁3/4: {e['strategy_id']} effective(隐式默认)不一致")
        if e["retained"] and e["derived"]:
            # retained 变体必须来自 _expand_variants 输出（seed 写入 DB 的正是该 vtext）
            _, text = _load_strategy_yaml(e["base_id"])
            rows = {sid: vt for sid, _, vt, _ in _expand_variants(e["base_id"], text)}
            if e["yaml_text"] != rows.get(e["strategy_id"]):
                fails.append(f"门禁3: {e['strategy_id']} 与 seed 展开文本不一致")

    # ---- 门禁 5：runtime binding 必须逐策略存在；找不到的历史事实只能标 unrecoverable ----
    binding_path = OUT / "runtime-bindings.yaml"
    if not binding_path.exists():
        fails.append("门禁5: 缺 runtime-bindings.yaml")
    else:
        bindings = yaml.safe_load(binding_path.read_text(encoding="utf-8")) or {}
        strategy_bindings = bindings.get("strategies") or {}
        missing = catalog_ids - set(strategy_bindings)
        if missing:
            fails.append(f"门禁5: 缺逐策略 runtime binding: {sorted(missing)}")
        for sid in sorted(catalog_ids & set(strategy_bindings)):
            item = strategy_bindings[sid]
            for key in ("rebalancer", "universe", "cost_model", "entrypoints"):
                if key not in item:
                    fails.append(f"门禁5: {sid} 缺 binding.{key}")
            if _contains_unknown(item):
                fails.append(f"门禁5: {sid} 含未解释的 unknown binding")

    # ---- 门禁 6：现有 artifact 有路径/hash；没有结果必须显式 not_available ----
    index_path = OUT / "result-index.csv"
    if not index_path.exists():
        fails.append("门禁6: 缺 result-index.csv")
    else:
        indexed_rows = list(csv.DictReader(index_path.open(encoding="utf-8")))
        indexed = {str(row.get("strategy_id") or "") for row in indexed_rows}
        if catalog_ids - indexed:
            fails.append(f"门禁6: 缺策略结果占位: {sorted(catalog_ids - indexed)}")
        for row in indexed_rows:
            artifact = row.get("artifact") or ""
            if artifact == "not_available":
                if row.get("verdict") != "not_available":
                    fails.append(f"门禁6: {row.get('strategy_id')} 缺结果未标 not_available")
                continue
            path = ROOT / artifact
            if not path.exists():
                fails.append(f"门禁6: artifact 不存在 {artifact}")
            elif row.get("sha256") != _sha256(path):
                fails.append(f"门禁6: artifact checksum 不一致 {artifact}")

    # ---- 门禁 7：migration-map 覆盖所有 old id 和 variant id ----
    migration_path = OUT / "migration-map.yaml"
    if not migration_path.exists():
        fails.append("门禁7: 缺 migration-map.yaml")
    else:
        migration = yaml.safe_load(migration_path.read_text(encoding="utf-8")) or {}
        mapped = set((migration.get("mapping") or {}).keys())
        if mapped != catalog_ids:
            fails.append(
                f"门禁7: migration-map 覆盖不一致；缺 {sorted(catalog_ids - mapped)}，"
                f"多 {sorted(mapped - catalog_ids)}"
            )

    # ---- 门禁 8：V2 CLI 对 old id 必须 fail-closed，并指向 migration-map ----
    try:
        from stockfu.backtest.v2_run import validate_v2_alpha_id
        for sid in sorted(catalog_ids):
            try:
                validate_v2_alpha_id(sid)
            except ValueError as exc:
                if "已归档" not in str(exc) or "migration-map" not in str(exc):
                    fails.append(f"门禁8: {sid} 未返回明确归档提示: {exc}")
            else:
                fails.append(f"门禁8: old id {sid} 被 V2 CLI 静默接受")
    except Exception as exc:
        fails.append(f"门禁8: 无法校验 V2 old-id fail-closed: {type(exc).__name__}: {exc}")

    print(f"校验完成：{len(entries)} 条展开配置，{len(fails)} 项失败")
    for f in fails:
        print(f"  ✗ {f}")
    return len(fails)


def main() -> int:
    ap = argparse.ArgumentParser(description="V1 策略归档生成与门禁校验（§17）")
    ap.add_argument("action", choices=("generate", "verify"))
    args = ap.parse_args()

    entries = _collect_entries()

    if args.action == "verify":
        return 1 if _verify(entries) else 0

    OUT.mkdir(parents=True, exist_ok=True)
    _write_strategy_sources()
    (OUT / "catalog.yaml").write_text(
        yaml.safe_dump(_render_catalog_yaml(entries), allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    _write_catalog_md(entries)
    _write_runtime_bindings(entries)
    (OUT / "migration-map.yaml").write_text(
        yaml.safe_dump({"schema_version": 1, "mapping": _expanded_migration_map(entries)},
                       allow_unicode=True, sort_keys=False),
                       encoding="utf-8")
    _write_result_index(entries)
    _write_checksums()
    print(f"已生成 {OUT}")
    fails = _verify(entries)
    print("门禁 1-8：" + ("全部通过 ✓" if fails == 0 else f"{fails} 项失败"))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
