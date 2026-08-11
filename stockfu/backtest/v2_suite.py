"""V2 正式分段回测编排与产物保留。

引擎负责一次独立回测；本模块负责把同一快照、同一代码版本和同一参数
分别运行于固定的三段样本，并为每段保留 summary、完整 checkpoint 和
append-only audit。每次 suite 使用新的 output_root，避免新结果覆盖旧结果。
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import traceback
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


# A monkeypatched runner is a useful unit-test seam but cannot be imported by a
# fresh worker process.  Keep a reference so tests and embedding callers that
# deliberately replace ``run`` retain the old in-process behavior.
_ORIGINAL_RUN = run


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


def _suite_worker(payload: dict, sender) -> None:
    """执行一条 suite item，并只把小型结果通过 IPC 返回。

    V2Result 含完整曲线、订单和诊断数组，不能跨进程 pickle 回父进程；
    worker 直接落 summary，父进程只读取这个小文件和 snapshot descriptor。
    worker 退出后由操作系统回收整个 Python heap/RSS。
    """
    try:
        result = run(payload["alpha_id"], **payload["run_kwargs"])
        summary = build_result_summary(
            result,
            payload["deployment"],
            payload["segment"],
            payload["history_origin"],
            Path(payload["summary_path"]),
            Path(payload["checkpoint_path"]),
            Path(payload["output_root"]),
        )
        _atomic_write_json(Path(payload["summary_path"]), summary)
        sender.send({
            "ok": True,
            "snapshot": result.manifest.get("data_snapshot"),
        })
    except BaseException as exc:  # child must report OOM/termination context when possible
        try:
            sender.send({
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            })
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        sender.close()


def _run_one_inline(payload: dict) -> tuple[dict | None, dict]:
    """测试 seam/显式禁用隔离时的单项执行。"""
    result = run(payload["alpha_id"], **payload["run_kwargs"])
    summary = build_result_summary(
        result,
        payload["deployment"],
        payload["segment"],
        payload["history_origin"],
        Path(payload["summary_path"]),
        Path(payload["checkpoint_path"]),
        Path(payload["output_root"]),
    )
    _atomic_write_json(Path(payload["summary_path"]), summary)
    return result.manifest.get("data_snapshot"), summary


def _run_one_isolated(payload: dict) -> tuple[dict | None, dict]:
    """启动一个干净子进程，避免 suite 内多项回测共享 Python 堆。"""
    # spawn 不继承父进程的 SQLAlchemy/SQLite 连接和 allocator 状态；Linux
    # 上也优先使用它，代价是每项多一次 import，换来明确的 RSS 上限。
    ctx = mp.get_context("spawn")
    receiver, sender = ctx.Pipe(duplex=False)
    process = ctx.Process(target=_suite_worker, args=(payload, sender))
    process.start()
    sender.close()
    process.join()
    message = receiver.recv() if receiver.poll() else None
    receiver.close()

    if not message:
        raise RuntimeError(
            "suite worker 未返回结果，"
            f"exitcode={process.exitcode}（可能被 OOM killer/外部信号终止）"
        )
    if not message.get("ok"):
        detail = message.get("traceback") or message.get("error") or "未知 worker 错误"
        raise RuntimeError(f"suite worker 执行失败: {detail}")
    if process.exitcode != 0:
        raise RuntimeError(f"suite worker 异常退出: exitcode={process.exitcode}")

    summary_path = Path(payload["summary_path"])
    if not summary_path.is_file():
        raise RuntimeError(f"suite worker 未生成 summary: {summary_path}")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    return message.get("snapshot"), summary


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
    isolate_processes: bool = True,
    resume_existing: bool = False,
) -> SegmentedBacktestSuite:
    """在固定样本区间上运行并保留一组 V2 部署。

    ``output_root`` 必须是本次 suite 的新目录。函数拒绝复用非空目录，
    以防不同代码/快照的结果静默覆盖；每段运行独立初始化账户和历史状态。
    默认每条 deployment/segment 在独立 worker 进程执行，worker 退出后由
    操作系统回收 Python heap；``isolate_processes=False`` 仅用于小型测试或
    明确需要在当前进程注入 runner 的场景。传入 ``resume_existing=True``
    时允许打开已有 suite，跳过已完成项，并从未完成项的 checkpoint 续跑。
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
    root_exists = root.exists() and any(root.iterdir())
    if root_exists and not resume_existing:
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

    if resume_existing:
        if not manifest_path.is_file():
            raise FileNotFoundError(
                f"resume_existing 要求 suite manifest 存在: {manifest_path}")
        old_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if old_manifest.get("kind") != "stockfu.v2.backtest.segment_suite":
            raise ValueError(f"suite manifest kind 不匹配: {manifest_path}")
        for field, expected in (
            ("observation_count", observation_count),
            ("checkpoint_every", checkpoint_every),
            ("canonical", canonical),
        ):
            if field in old_manifest and old_manifest[field] != expected:
                raise ValueError(
                    f"续跑 suite 的 {field} 不匹配: "
                    f"已有={old_manifest[field]!r}, 当前={expected!r}"
                )
        old_entries = {
            (str(item.get("variant_id")), str(item.get("alpha_id")),
             str(item.get("segment_id"))): item
            for item in old_manifest.get("entries", [])
        }
        expected_keys = {
            (str(item["variant_id"]), str(item["alpha_id"]), str(item["segment_id"]))
            for item in entries
        }
        if set(old_entries) != expected_keys:
            raise ValueError(
                "续跑 suite 的 deployment/segment 集合不匹配；"
                "请使用原始 alpha、variant 和 segments 参数"
            )
        for item in entries:
            key = (str(item["variant_id"]), str(item["alpha_id"]),
                   str(item["segment_id"]))
            old = old_entries[key]
            summary_file = root / item["summary"]
            checkpoint_file = root / item["checkpoint"]
            complete = (
                old.get("status") == "complete"
                and summary_file.is_file()
                and checkpoint_file.is_file()
            )
            if complete:
                for field in ("status", "run_id", "effective_eval_end", "metrics"):
                    if field in old:
                        item[field] = old[field]
            else:
                item["status"] = "pending"
        suite_manifest = dict(old_manifest)
        suite_manifest.update({
            "status": "running",
            "entries": entries,
            "checkpoint_every": checkpoint_every,
            "observation_count": observation_count,
            "canonical": canonical,
            "resumed_at": datetime.now(timezone.utc).isoformat(),
        })
        suite_manifest.pop("error", None)
    else:
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
            "snapshot": snapshot,
        }
    _atomic_write_json(manifest_path, suite_manifest)

    results: list[SegmentedBacktestRun] = []
    shared_snapshot = snapshot or suite_manifest.get("snapshot")
    use_isolation = isolate_processes and run is _ORIGINAL_RUN
    suite_manifest["process_isolation"] = use_isolation
    _atomic_write_json(manifest_path, suite_manifest)
    try:
        for index, (deployment, segment, summary_path, checkpoint_path) in enumerate(run_specs):
            entry = entries[index]
            if entry.get("status") == "complete":
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                results.append(SegmentedBacktestRun(
                    deployment=deployment,
                    segment=segment,
                    summary_path=summary_path,
                    checkpoint_path=checkpoint_path,
                    summary=summary,
                ))
                continue
            entry["status"] = "running"
            _atomic_write_json(manifest_path, suite_manifest)
            segment_history_origin = history_origin or segment.history_origin()
            payload = {
                "alpha_id": deployment.alpha_id,
                "deployment": deployment,
                "segment": segment,
                "history_origin": segment_history_origin,
                "summary_path": str(summary_path),
                "checkpoint_path": str(checkpoint_path),
                "output_root": str(root),
                "run_kwargs": {
                    "eval_start": segment.eval_start,
                    "eval_end": segment.eval_end,
                    "codes": codes,
                    "portfolio_id": deployment.portfolio_id,
                    "risk_id": deployment.risk_id,
                    "history_origin": segment_history_origin,
                    "initial_cash": initial_cash,
                    "observation_count": observation_count,
                    "universe_rules": universe_rules,
                    "checkpoint_path": str(checkpoint_path),
                    "resume_from": (
                        str(checkpoint_path) if checkpoint_path.is_file() else None
                    ),
                    "checkpoint_every": checkpoint_every,
                    "snapshot": shared_snapshot,
                    "snapshots_dir": snapshots_dir,
                    "canonical": canonical,
                    "segment_id": segment.segment_id,
                },
            }
            if use_isolation:
                worker_snapshot, summary = _run_one_isolated(payload)
            else:
                worker_snapshot, summary = _run_one_inline(payload)
            if shared_snapshot is None:
                shared_snapshot = worker_snapshot
            suite_manifest["snapshot"] = shared_snapshot
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
