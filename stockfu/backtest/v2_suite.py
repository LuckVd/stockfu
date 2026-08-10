"""V2 正式分段回测编排与产物保留。

引擎负责一次独立回测；本模块负责把同一快照、同一代码版本和同一参数
分别运行于固定的三段样本，并为每段保留 summary、完整 checkpoint 和
append-only audit。每次 suite 使用新的 output_root，避免新结果覆盖旧结果。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from stockfu.backtest.segments import (
    DEFAULT_OBSERVATION_COUNT,
    BacktestSegment,
    is_complete_segment_set,
    resolve_segments,
)
from stockfu.backtest.v2_engine import V2Result
from stockfu.backtest.v2_run import DEFAULT_V2_DEPLOYMENTS, run
from stockfu.services.universe import UniverseRules


RESEARCH_FREQUENCIES = ("monthly", "weekly", "daily")
_ALPHA_DISPLAY_ORDER = (
    "multi_factor_v2",
    "value_ep_bp_v2",
    "dividend_income_v2",
    "low_volatility_pure_v2",
    "defensive_low_beta_v2",
    "momentum_jt_v2",
    "fifty_two_week_high_v2",
    "trend_following_v2",
    "reversal_jl_v2",
    "rsi_reversal_v2",
)


@dataclass(frozen=True)
class V2Deployment:
    """一条待跑的 alpha + portfolio + risk 部署。"""

    alpha_id: str
    variant_id: str = "custom"
    portfolio_id: str | None = None
    risk_id: str | None = None


@dataclass(frozen=True)
class SegmentedBacktestRun:
    deployment: V2Deployment
    segment: BacktestSegment
    summary_path: Path
    checkpoint_path: Path
    summary: dict


@dataclass(frozen=True)
class SegmentedBacktestSuite:
    manifest_path: Path
    runs: tuple[SegmentedBacktestRun, ...]


def research_alpha_ids() -> tuple[str, ...]:
    """当前十策略研究集合；集中在编排层供批跑脚本复用。"""
    return _ALPHA_DISPLAY_ORDER


def resolve_alpha_ids(selection: str | Iterable[str] | None = None) -> tuple[str, ...]:
    """解析 alpha 选择；``all`` 代表当前十策略研究集合。"""
    if selection is None:
        return research_alpha_ids()
    if isinstance(selection, str):
        raw = selection.strip()
        if not raw or raw.lower() in {"all", "全部", "十策略"}:
            return research_alpha_ids()
        values = [part.strip() for part in raw.split(",") if part.strip()]
    else:
        values = [str(part).strip() for part in selection if str(part).strip()]
    if not values:
        return research_alpha_ids()
    # 明确点名时允许任意已注册 V2 alpha；最终由 validate_v2_alpha_id 负责
    # 给出统一的配置错误。这样未纳入十策略邮件的 canonical alpha 也能复用
    # 同一套三段编排。
    return tuple(dict.fromkeys(values))


def research_deployments(
    alpha_ids: str | Iterable[str] | None = None,
    frequencies: str | Iterable[str] | None = None,
) -> tuple[V2Deployment, ...]:
    """生成十策略三频部署矩阵，供正式批跑使用。

    daily 使用 ``DEFAULT_V2_DEPLOYMENTS``；周/月使用研究阶段已固定的
    bare policy。risk 不随频率改变，趋势策略始终保留 trend_trailing_v2。
    """
    alphas = resolve_alpha_ids(alpha_ids)
    if frequencies is None:
        freq_values = RESEARCH_FREQUENCIES
    elif isinstance(frequencies, str):
        raw = frequencies.strip()
        freq_values = RESEARCH_FREQUENCIES if raw.lower() in {"all", "全部"} else tuple(
            part.strip() for part in raw.split(",") if part.strip()
        )
    else:
        freq_values = tuple(str(value).strip() for value in frequencies if str(value).strip())
    unknown = sorted(set(freq_values) - set(RESEARCH_FREQUENCIES))
    if unknown:
        raise ValueError(
            f"未知回测频率: {unknown}; 可选: {', '.join(RESEARCH_FREQUENCIES)}"
        )

    deployments: list[V2Deployment] = []
    for frequency in RESEARCH_FREQUENCIES:
        if frequency not in freq_values:
            continue
        for alpha_id in alphas:
            if frequency == "daily":
                portfolio_id = None
            elif frequency == "weekly":
                portfolio_id = (
                    "pf_weekly_top10_v2"
                    if alpha_id == "dividend_income_v2"
                    else "pf_weekly_top15_v2"
                )
            else:
                portfolio_id = (
                    "pf_monthly_top10_v2"
                    if alpha_id == "dividend_income_v2"
                    else "pf_monthly_top15_v2"
                )
            deployments.append(V2Deployment(
                alpha_id=alpha_id,
                variant_id=frequency,
                portfolio_id=portfolio_id,
                risk_id=DEFAULT_V2_DEPLOYMENTS.get(alpha_id, {}).get("risk_id"),
            ))
    return tuple(deployments)


def _safe_part(value: str) -> str:
    value = str(value).strip()
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError(f"非法回测产物路径片段: {value!r}")
    return value


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _resolved_policy_id(deployment: V2Deployment) -> tuple[str, str]:
    defaults = DEFAULT_V2_DEPLOYMENTS.get(deployment.alpha_id, {})
    return (
        deployment.portfolio_id or defaults.get("portfolio_id", "cn_equity_top15_v2"),
        deployment.risk_id or defaults.get("risk_id", "no_overlay_v1"),
    )


def build_result_summary(
    result: V2Result,
    deployment: V2Deployment,
    segment: BacktestSegment,
    history_origin: date,
    summary_path: Path,
    checkpoint_path: Path,
    output_root: Path,
) -> dict:
    """将一次 V2Result 压缩成可检索摘要；完整数组仍在 checkpoint state。"""
    portfolio_id, risk_id = _resolved_policy_id(deployment)
    coverage = result.manifest.get("data_coverage") or {}
    try:
        summary_rel = str(summary_path.relative_to(output_root))
        checkpoint_rel = str(checkpoint_path.relative_to(output_root))
    except ValueError:
        summary_rel = str(summary_path)
        checkpoint_rel = str(checkpoint_path)
    return {
        "schema_version": 1,
        "segment": segment.to_dict(),
        "deployment": {
            "alpha_id": deployment.alpha_id,
            "variant_id": deployment.variant_id,
            "portfolio_id": portfolio_id,
            "risk_id": risk_id,
        },
        "sample": {
            "requested_eval_start": segment.eval_start.isoformat(),
            "requested_eval_end": segment.eval_end.isoformat(),
            "effective_eval_end": coverage.get("effective_eval_end"),
            "data_end": coverage.get("data_end"),
            "truncated": bool(coverage.get("truncated")),
            "history_origin": history_origin.isoformat(),
            "observation_count": result.manifest.get("observation_count"),
            "formal_start": result.manifest.get("formal_start"),
            "formal_days": result.formal_summary.get("n_days"),
        },
        "metrics": result.metrics,
        "formal_summary": result.formal_summary,
        "observation_summary": result.observation_summary,
        "run_id": result.manifest.get("run_id"),
        "first_trade_date": result.first_trade_date,
        "last_trade_date": result.last_trade_date,
        "n_trades": len(result.trades),
        "risk_metrics": result.manifest.get("risk_metrics"),
        "score_diagnostics": result.score_diagnostics,
        "artifacts": {
            "summary": summary_rel,
            "checkpoint": checkpoint_rel,
            "audit": checkpoint_rel + ".audit.jsonl",
        },
    }


def run_segmented_backtests(
    deployments: Sequence[V2Deployment],
    *,
    output_root: str | Path,
    segments: str | Iterable[str] | None = None,
    codes: list[str] | None = None,
    universe_rules: UniverseRules | None = None,
    history_origin: date | None = None,
    initial_cash: float | None = None,
    observation_count: int | None = DEFAULT_OBSERVATION_COUNT,
    checkpoint_every: int = 20,
    snapshot: dict | None = None,
    snapshots_dir: str | None = None,
    canonical: bool = False,
) -> SegmentedBacktestSuite:
    """在固定样本区间上运行并保留一组 V2 部署。

    ``output_root`` 必须是本次 suite 的新目录。函数拒绝复用非空目录，
    以防不同代码/快照的结果静默覆盖；每段运行独立初始化账户和历史状态。
    """
    deployments = tuple(deployments)
    if not deployments:
        raise ValueError("至少需要一条 V2 deployment")
    selected_segments = resolve_segments(segments)
    if canonical and not is_complete_segment_set(selected_segments):
        raise ValueError("canonical 正式回测必须同时覆盖 full、2013-2019、2020-2026 三段")
    if observation_count is None:
        observation_count = DEFAULT_OBSERVATION_COUNT
    if observation_count < 0:
        raise ValueError("observation_count 不得为负")
    if canonical:
        # suite 自己也会写 suite.json；canonical 门禁必须先于这些副作用。
        from stockfu.backtest.v2_engine import canonical_preflight

        canonical_preflight(canonical)

    root = Path(output_root)
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(
            f"回测 suite 输出目录必须为空以保留历史产物: {root}; 请新建 run-* 目录"
        )
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "suite.json"
    entries: list[dict] = []
    run_specs: list[tuple[V2Deployment, BacktestSegment, Path, Path]] = []
    seen: set[tuple[str, str, str]] = set()
    for deployment in deployments:
        variant_id = _safe_part(deployment.variant_id)
        alpha_id = _safe_part(deployment.alpha_id)
        for segment in selected_segments:
            key = (variant_id, alpha_id, segment.segment_id)
            if key in seen:
                raise ValueError(f"重复回测 deployment: {key}")
            seen.add(key)
            artifact_dir = root / segment.segment_id / variant_id / alpha_id
            checkpoint_path = artifact_dir / f"{alpha_id}.checkpoint.json"
            summary_path = artifact_dir / f"{alpha_id}.json"
            run_specs.append((deployment, segment, summary_path, checkpoint_path))
            entries.append({
                "segment_id": segment.segment_id,
                "variant_id": variant_id,
                "alpha_id": alpha_id,
                "summary": str(summary_path.relative_to(root)),
                "checkpoint": str(checkpoint_path.relative_to(root)),
                "audit": str(checkpoint_path.relative_to(root)) + ".audit.jsonl",
                "status": "pending",
            })

    suite_manifest = {
        "schema_version": 1,
        "kind": "stockfu.v2.backtest.segment_suite",
        "status": "running",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "output_root": str(root),
        "segments": [segment.to_dict() for segment in selected_segments],
        "observation_count": observation_count,
        "checkpoint_every": checkpoint_every,
        "canonical": canonical,
        "entries": entries,
    }
    _atomic_write_json(manifest_path, suite_manifest)

    results: list[SegmentedBacktestRun] = []
    shared_snapshot = snapshot
    try:
        for index, (deployment, segment, summary_path, checkpoint_path) in enumerate(run_specs):
            entry = entries[index]
            entry["status"] = "running"
            _atomic_write_json(manifest_path, suite_manifest)
            segment_history_origin = history_origin or segment.history_origin()
            result = run(
                deployment.alpha_id,
                eval_start=segment.eval_start,
                eval_end=segment.eval_end,
                codes=codes,
                portfolio_id=deployment.portfolio_id,
                risk_id=deployment.risk_id,
                history_origin=segment_history_origin,
                initial_cash=initial_cash,
                observation_count=observation_count,
                universe_rules=universe_rules,
                checkpoint_path=str(checkpoint_path),
                checkpoint_every=checkpoint_every,
                snapshot=shared_snapshot,
                snapshots_dir=snapshots_dir,
                canonical=canonical,
                segment_id=segment.segment_id,
            )
            if shared_snapshot is None:
                shared_snapshot = result.manifest.get("data_snapshot")
            summary = build_result_summary(
                result, deployment, segment, segment_history_origin,
                summary_path, checkpoint_path, root,
            )
            _atomic_write_json(summary_path, summary)
            entry.update({
                "status": "complete",
                "run_id": summary.get("run_id"),
                "effective_eval_end": summary["sample"].get("effective_eval_end"),
                "metrics": summary.get("metrics"),
            })
            _atomic_write_json(manifest_path, suite_manifest)
            results.append(SegmentedBacktestRun(
                deployment=deployment,
                segment=segment,
                summary_path=summary_path,
                checkpoint_path=checkpoint_path,
                summary=summary,
            ))
    except Exception as exc:
        suite_manifest["status"] = "failed"
        suite_manifest["error"] = f"{type(exc).__name__}: {exc}"
        _atomic_write_json(manifest_path, suite_manifest)
        raise

    suite_manifest["status"] = "complete"
    suite_manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
    suite_manifest["snapshot_id"] = shared_snapshot.get("snapshot_id") if shared_snapshot else None
    _atomic_write_json(manifest_path, suite_manifest)
    return SegmentedBacktestSuite(manifest_path, tuple(results))
