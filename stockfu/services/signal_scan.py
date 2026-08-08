"""沪深300+中证500策略评分扫描、逐股订阅与按需 LLM 分析。"""
from __future__ import annotations

import json
import time
from datetime import date, datetime
from threading import Lock
from typing import Any, Iterable

from sqlmodel import select

from stockfu.db import session_scope
from stockfu.models import (
    Asset,
    FactorSignal,
    LlmSignalAnalysis,
    SecurityMaster,
    SignalScanRun,
    StockSignalSubscription,
    Strategy,
)
from stockfu.services.index_universe import (
    HISTORICAL_INDEX_CODES,
    current_member_codes,
    current_member_snapshot,
    normalize_code,
)

PROMPT_VERSION = "signal-score-v1"
_SCHEMA_READY = False
_SCHEMA_LOCK = Lock()


def ensure_signal_schema() -> None:
    """升级现有主库时只补本功能的新表，不要求用户重新执行完整初始化。"""
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    from stockfu.db import engine

    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return
        for model in (SignalScanRun, FactorSignal, LlmSignalAnalysis, StockSignalSubscription):
            model.__table__.create(engine, checkfirst=True)
        _SCHEMA_READY = True


def score_to_100(
    raw_score: float | int | None,
    score_full: float | int | None,
    *,
    risk_vetoed: bool = False,
) -> float | None:
    """策略原始中心分映射到 0–100；50 中性，±score_full 对应 0/100。"""
    if raw_score is None:
        return None
    if risk_vetoed:
        return 0.0
    try:
        raw = float(raw_score)
        full = abs(float(score_full or 20.0))
    except (TypeError, ValueError):
        return None
    if full <= 0:
        full = 20.0
    return round(max(0.0, min(100.0, 50.0 + raw / full * 50.0)), 2)


def _json_load(raw: str | None, default):
    try:
        return json.loads(raw or "")
    except (TypeError, ValueError):
        return default


def _stock_names(codes: Iterable[str]) -> dict[str, str]:
    wanted = sorted(set(codes))
    if not wanted:
        return {}
    names: dict[str, str] = {}
    with session_scope() as s:
        for row in s.exec(select(SecurityMaster).where(SecurityMaster.code.in_(wanted))).all():
            names[row.code] = row.name or ""
        for row in s.exec(select(Asset).where(Asset.code.in_(wanted))).all():
            if row.name:
                names.setdefault(row.code, row.name)
    return names


def strategy_options() -> list[dict[str, str]]:
    with session_scope() as s:
        rows = s.exec(select(Strategy).order_by(Strategy.strategy_id)).all()
    return [{"strategy_id": row.strategy_id, "name": row.name or row.strategy_id} for row in rows]


def strategy_operator_ids(strategy_ids: list[str]) -> set[str]:
    """读取动态策略的数据依赖，用于调度器决定额外刷新哪些慢数据。"""
    import yaml

    with session_scope() as s:
        rows = s.exec(select(Strategy).where(Strategy.strategy_id.in_(strategy_ids))).all()
    operators: set[str] = set()
    for row in rows:
        config = yaml.safe_load(row.config) or {}
        for spec in config.get("operators") or []:
            operator_id = str((spec or {}).get("id") or "").strip()
            if operator_id:
                operators.add(operator_id)
    return operators


def signal_config_view() -> dict[str, Any]:
    from stockfu.config import get_signal_config

    return {**get_signal_config(), "available_strategies": strategy_options()}


def update_signal_config(data: dict) -> dict[str, Any]:
    from stockfu.config import set_signal_config

    if "strategy_ids" in data:
        if not isinstance(data["strategy_ids"], list):
            raise ValueError("strategy_ids 必须是数组")
        available = {row["strategy_id"] for row in strategy_options()}
        requested = [str(value).strip() for value in data["strategy_ids"] if str(value).strip()]
        unknown = sorted(set(requested) - available)
        if unknown:
            raise ValueError(f"未知 strategy_id: {unknown}")
    set_signal_config(data)
    return signal_config_view()


def subscription_rows(as_of: date) -> list[dict[str, Any]]:
    """返回当前指数成分和逐股开关；无订阅行默认均关闭。"""
    snapshots = current_member_snapshot(as_of, HISTORICAL_INDEX_CODES)
    codes = current_member_codes(as_of, HISTORICAL_INDEX_CODES)
    memberships: dict[str, list[str]] = {code: [] for code in codes}
    for index_code, snapshot in snapshots.items():
        for code in snapshot["members"]:
            memberships.setdefault(code, []).append(index_code)
    names = _stock_names(codes)
    with session_scope() as s:
        existing = {
            row.asset_code: row
            for row in s.exec(select(StockSignalSubscription).where(
                StockSignalSubscription.asset_code.in_(codes)
            )).all()
        }
    return [{
        "code": code,
        "name": names.get(code, ""),
        "index_codes": memberships.get(code, []),
        "factor_mail_enabled": bool(existing.get(code) and existing[code].factor_mail_enabled),
        "llm_enabled": bool(existing.get(code) and existing[code].llm_enabled),
    } for code in codes]


def set_subscriptions(updates: list[dict]) -> dict[str, int]:
    """批量 upsert 逐股开关；未传字段保持原值。"""
    normalized: dict[str, dict] = {}
    for item in updates or []:
        code = normalize_code(item.get("code"))
        if not code:
            continue
        normalized[code] = item
    now = datetime.now()
    with session_scope() as s:
        for code, item in normalized.items():
            row = s.get(StockSignalSubscription, code)
            row = row or StockSignalSubscription(asset_code=code)
            if "factor_mail_enabled" in item:
                row.factor_mail_enabled = bool(item["factor_mail_enabled"])
            if "llm_enabled" in item:
                row.llm_enabled = bool(item["llm_enabled"])
            row.updated_at = now
            s.add(row)
        s.commit()
    return {"updated": len(normalized)}


def enabled_subscription_codes(*, factor_mail: bool = False, llm: bool = False) -> list[str]:
    if not factor_mail and not llm:
        return []
    with session_scope() as s:
        stmt = select(StockSignalSubscription)
        if factor_mail and llm:
            stmt = stmt.where(
                StockSignalSubscription.factor_mail_enabled == True,  # noqa: E712
                StockSignalSubscription.llm_enabled == True,  # noqa: E712
            )
        elif factor_mail:
            stmt = stmt.where(StockSignalSubscription.factor_mail_enabled == True)  # noqa: E712
        else:
            stmt = stmt.where(StockSignalSubscription.llm_enabled == True)  # noqa: E712
        rows = s.exec(stmt).all()
    return sorted({row.asset_code for row in rows})


def _strategy_meta(strategy_ids: list[str]) -> dict[str, dict[str, Any]]:
    """策略显示名和原始分满刻度；编译失败在正式 evaluate 前尽早暴露。"""
    from stockfu.ai.operators.registry import discover_and_register
    from stockfu.ai.operators.runner import compile_strategy
    import hashlib

    discover_and_register()
    with session_scope() as s:
        rows = {
            row.strategy_id: row
            for row in s.exec(select(Strategy).where(Strategy.strategy_id.in_(strategy_ids))).all()
        }
    missing = [sid for sid in strategy_ids if sid not in rows]
    if missing:
        raise ValueError(f"未知 strategy_id: {missing}")
    out: dict[str, dict[str, Any]] = {}
    for sid in strategy_ids:
        compiled = compile_strategy(rows[sid].config, strategy_id=sid)
        out[sid] = {
            "name": rows[sid].name or compiled.name or sid,
            "score_full": float(compiled.debounce_params.score_full or 20.0),
            "fingerprint": hashlib.sha1(rows[sid].config.encode("utf-8")).hexdigest()[:16],
        }
    return out


def _normalized_cells(report: dict, meta: dict[str, dict[str, Any]]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in report.get("matrix") or []:
        per: dict[str, dict] = {}
        for sid, cell in (row.get("per_strategy") or {}).items():
            raw = cell.get("total_score")
            per[sid] = {
                **cell,
                "score": score_to_100(
                    raw,
                    meta[sid]["score_full"],
                    risk_vetoed=bool(cell.get("risk_vetoed")),
                ),
            }
        out[row["code"]] = per
    return out


def _persist_factor_signals(
    run_id: int,
    signal_date: date,
    cells_by_code: dict[str, dict],
    meta: dict[str, dict[str, Any]],
) -> int:
    completed = 0
    with session_scope() as s:
        for code, per in cells_by_code.items():
            for sid, cell in per.items():
                error = str(cell.get("error") or "")
                if not error and cell.get("score") is not None:
                    completed += 1
                s.add(FactorSignal(
                    scan_run_id=run_id,
                    signal_date=signal_date,
                    asset_code=code,
                    strategy_id=sid,
                    strategy_name=meta[sid]["name"],
                    strategy_fingerprint=meta[sid].get("fingerprint", ""),
                    score_full=meta[sid]["score_full"],
                    score=cell.get("score"),
                    raw_score=cell.get("total_score"),
                    confidence=cell.get("confidence"),
                    legacy_signal=str(cell.get("signal") or ""),
                    risk_vetoed=bool(cell.get("risk_vetoed")),
                    factors_json=json.dumps(cell.get("factors") or {}, ensure_ascii=False),
                    details_json=json.dumps(cell.get("opinions") or [], ensure_ascii=False),
                    error=error,
                ))
        s.commit()
    return completed


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value in (None, ""):
        return []
    return [str(value).strip()]


def analyze_with_llm(
    code: str,
    name: str,
    signal_date: date,
    strategy_cells: dict[str, dict],
) -> dict[str, Any]:
    """单次 OpenAI-compatible 调用，返回独立 0–100 分析。"""
    from stockfu.ai.client import chat_json
    from stockfu.config import get_llm_model

    views = []
    for sid, cell in strategy_cells.items():
        views.append({
            "strategy_id": sid,
            "score": cell.get("score"),
            "raw_score": cell.get("total_score"),
            "confidence": cell.get("confidence"),
            "factors": cell.get("factors") or {},
            "error": cell.get("error") or None,
        })
    system = (
        "你是审慎的A股信号分析师。根据给定的量化策略结果给出独立评分，"
        "不得假设用户持仓，不得承诺收益，不得编造未提供的数据。"
        "只输出合法JSON对象："
        '{"score":0到100的数字,"summary":"2到4句摘要",'
        '"reasons":["理由"],"risks":["风险"]}。'
        "50表示中性，越高越偏向买点，越低越偏向卖点。"
    )
    user = json.dumps({
        "signal_date": signal_date.isoformat(),
        "code": code,
        "name": name,
        "strategy_results": views,
    }, ensure_ascii=False, indent=2)
    started = time.monotonic()
    data = chat_json(system, user, temperature=0.2, max_tokens=700, timeout=60.0)
    try:
        score = max(0.0, min(100.0, float(data.get("score"))))
    except (TypeError, ValueError):
        raise ValueError("LLM 未返回合法的 0–100 score")
    return {
        "model": get_llm_model(),
        "score": round(score, 2),
        "summary": str(data.get("summary") or "").strip(),
        "reasons": _string_list(data.get("reasons")),
        "risks": _string_list(data.get("risks")),
        "latency_ms": int((time.monotonic() - started) * 1000),
    }


def _persist_llm(
    run_id: int,
    signal_date: date,
    code: str,
    source_cells: dict[str, dict],
    result: dict | None,
    *,
    error: str = "",
) -> None:
    from stockfu.config import get_llm_model

    source_scores = {sid: cell.get("score") for sid, cell in source_cells.items()}
    row = LlmSignalAnalysis(
        scan_run_id=run_id,
        signal_date=signal_date,
        asset_code=code,
        model=(result or {}).get("model") or get_llm_model(),
        score=(result or {}).get("score"),
        summary=(result or {}).get("summary") or "",
        reasons_json=json.dumps((result or {}).get("reasons") or [], ensure_ascii=False),
        risks_json=json.dumps((result or {}).get("risks") or [], ensure_ascii=False),
        source_scores_json=json.dumps(source_scores, ensure_ascii=False),
        prompt_version=PROMPT_VERSION,
        status="failed" if error else "success",
        latency_ms=(result or {}).get("latency_ms"),
        error=error,
    )
    with session_scope() as s:
        s.add(row)
        s.commit()


def run_signal_scan(
    signal_date: date,
    *,
    strategy_ids: list[str] | None = None,
    factor_enabled: bool | None = None,
    llm_enabled: bool | None = None,
) -> dict[str, Any]:
    """运行一次可审计扫描；因子全量、LLM 仅逐股启用。"""
    from stockfu.config import (
        get_signal_factor_enabled,
        get_signal_llm_enabled,
        get_signal_strategy_ids,
    )
    from stockfu.services.evaluator import evaluate

    factor_on = get_signal_factor_enabled() if factor_enabled is None else factor_enabled
    llm_on = get_signal_llm_enabled() if llm_enabled is None else llm_enabled
    strategies = list(dict.fromkeys(strategy_ids or get_signal_strategy_ids()))
    if not strategies:
        raise ValueError("未配置任何信号策略")

    snapshots = current_member_snapshot(signal_date, HISTORICAL_INDEX_CODES)
    universe = current_member_codes(signal_date, HISTORICAL_INDEX_CODES)
    llm_subscribed = enabled_subscription_codes(llm=True) if llm_on else []
    llm_codes = sorted(set(universe) & set(llm_subscribed))
    eval_codes = universe if factor_on else llm_codes
    meta = _strategy_meta(strategies)

    with session_scope() as s:
        run = SignalScanRun(
            signal_date=signal_date,
            index_codes_json=json.dumps(snapshots, ensure_ascii=False),
            universe_json=json.dumps(universe, ensure_ascii=False),
            strategy_ids_json=json.dumps(strategies, ensure_ascii=False),
            universe_size=len(universe),
            factor_expected=len(universe) * len(strategies) if factor_on else 0,
            llm_requested=len(llm_codes),
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        run_id = int(run.id)

    try:
        cells_by_code: dict[str, dict] = {}
        if eval_codes:
            report = evaluate(eval_codes, strategies, signal_date, write_cache=True)
            cells_by_code = _normalized_cells(report, meta)

        factor_completed = 0
        if factor_on:
            factor_completed = _persist_factor_signals(
                run_id, signal_date, cells_by_code, meta,
            )

        names = _stock_names(llm_codes)
        llm_completed = 0
        if llm_codes:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="signal-llm") as pool:
                futures = {
                    pool.submit(
                        analyze_with_llm,
                        code,
                        names.get(code, ""),
                        signal_date,
                        cells_by_code.get(code) or {},
                    ): code
                    for code in llm_codes
                }
                for future in as_completed(futures):
                    code = futures[future]
                    source = cells_by_code.get(code) or {}
                    try:
                        result = future.result()
                        _persist_llm(run_id, signal_date, code, source, result)
                        llm_completed += 1
                    except Exception as exc:  # noqa: BLE001
                        _persist_llm(
                            run_id, signal_date, code, source, None,
                            error=f"{type(exc).__name__}: {exc}",
                        )

        factor_expected = len(universe) * len(strategies) if factor_on else 0
        failures = (factor_expected - factor_completed) + (len(llm_codes) - llm_completed)
        status = "success" if failures == 0 else "partial"
        if factor_on and factor_expected and factor_completed == 0:
            status = "failed"
        with session_scope() as s:
            row = s.get(SignalScanRun, run_id)
            row.status = status
            row.factor_completed = factor_completed
            row.llm_completed = llm_completed
            row.error = "" if failures == 0 else f"未完成结果 {failures} 条"
            row.finished_at = datetime.now()
            s.add(row)
            s.commit()
    except Exception as exc:
        with session_scope() as s:
            row = s.get(SignalScanRun, run_id)
            row.status = "failed"
            row.error = f"{type(exc).__name__}: {exc}"
            row.finished_at = datetime.now()
            s.add(row)
            s.commit()
        raise

    return signal_report(run_id=run_id, subscribed_only=False)


def latest_scan_run() -> SignalScanRun | None:
    with session_scope() as s:
        return s.exec(select(SignalScanRun).where(
            SignalScanRun.status.in_(["success", "partial"])
        ).order_by(SignalScanRun.signal_date.desc(), SignalScanRun.id.desc())).first()


def signal_report(
    *,
    run_id: int | None = None,
    subscribed_only: bool = True,
) -> dict[str, Any]:
    """读取批次报告；邮件默认仅返回逐股开启的内容。"""
    with session_scope() as s:
        run = s.get(SignalScanRun, run_id) if run_id is not None else s.exec(
            select(SignalScanRun).where(
                SignalScanRun.status.in_(["success", "partial"])
            ).order_by(SignalScanRun.signal_date.desc(), SignalScanRun.id.desc())
        ).first()
        if run is None:
            return {"status": "none", "rows": []}
        factor_rows = s.exec(select(FactorSignal).where(
            FactorSignal.scan_run_id == run.id
        ).order_by(FactorSignal.asset_code, FactorSignal.strategy_id)).all()
        llm_rows = s.exec(select(LlmSignalAnalysis).where(
            LlmSignalAnalysis.scan_run_id == run.id
        ).order_by(LlmSignalAnalysis.asset_code)).all()
        subscriptions = {
            row.asset_code: row
            for row in s.exec(select(StockSignalSubscription)).all()
        }

    universe = _json_load(run.universe_json, [])
    names = _stock_names(universe)
    by_code: dict[str, dict] = {}
    for code in universe:
        sub = subscriptions.get(code)
        factor_mail = bool(sub and sub.factor_mail_enabled and run.factor_expected > 0)
        llm_enabled = bool(sub and sub.llm_enabled and run.llm_requested > 0)
        if subscribed_only and not (factor_mail or llm_enabled):
            continue
        by_code[code] = {
            "code": code,
            "name": names.get(code, ""),
            "factor_mail_enabled": factor_mail,
            "llm_enabled": llm_enabled,
            "strategies": [],
            "llm": None,
        }
    for row in factor_rows:
        if row.asset_code not in by_code:
            continue
        by_code[row.asset_code]["strategies"].append({
            "strategy_id": row.strategy_id,
            "strategy_name": row.strategy_name,
            "strategy_fingerprint": row.strategy_fingerprint,
            "score_full": row.score_full,
            "score": row.score,
            "raw_score": row.raw_score,
            "confidence": row.confidence,
            "legacy_signal": row.legacy_signal,
            "risk_vetoed": row.risk_vetoed,
            "factors": _json_load(row.factors_json, {}),
            "error": row.error or None,
        })
    for row in llm_rows:
        if row.asset_code not in by_code:
            continue
        by_code[row.asset_code]["llm"] = {
            "model": row.model,
            "score": row.score,
            "summary": row.summary,
            "reasons": _json_load(row.reasons_json, []),
            "risks": _json_load(row.risks_json, []),
            "status": row.status,
            "error": row.error or None,
        }
    rows = list(by_code.values())
    rows.sort(key=lambda item: (
        -max([s["score"] for s in item["strategies"] if s["score"] is not None] or [-1]),
        item["code"],
    ))
    return {
        "status": run.status,
        "run_id": run.id,
        "signal_date": run.signal_date.isoformat(),
        "universe_size": run.universe_size,
        "strategy_ids": _json_load(run.strategy_ids_json, []),
        "factor_expected": run.factor_expected,
        "factor_completed": run.factor_completed,
        "llm_requested": run.llm_requested,
        "llm_completed": run.llm_completed,
        "error": run.error or None,
        "rows": rows,
    }
