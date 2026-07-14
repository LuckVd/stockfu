"""算子级回测缓存(operator_result 表)的读写 + 指纹计算。

去持仓依赖后,所有算子(math/llm)是纯市场数据函数 f(code, as_of, [params/prompt/temp]),
同输入全局任意复用(跨策略/跨回测)。本模块提供 read-through 缓存的存取原语,
由 runner.CompiledStrategy.analyze 在算子循环里调用。

指纹(fingerprint)编码"影响算子输出的非(code,as_of)输入":
  math: hash(params)               —— 窗口/周期等
  llm:  hash(prompt + temperature) —— prompt 从 DB 加载,改了自动失效;回测 temp=0 固定
holding 不进指纹(信号层已去持仓依赖)。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Iterable

from sqlmodel import select

from stockfu.ai.operators.base import OpResult
from stockfu.db import session_scope
from stockfu.models import OperatorResult


def compute_fingerprint(op_type: str, *, version: int = 1,
                        params: dict | None = None,
                        prompt: str | None = None,
                        temperature: float | None = None) -> str:
    """算子输入指纹(16 位 sha1)。code/as_of 不进(已在表 key 列);holding 不进(去持仓)。"""
    if op_type == "llm":
        spec = {"version": version, "prompt": prompt or "", "temperature": temperature}
    else:  # math
        spec = {"version": version, "params": params or {}}
    raw = json.dumps(spec, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _is_failure(r: OpResult) -> bool:
    """失败结果(置信 0 + reasoning 以方括号开头)不落库,重跑时重试(沿用原 classic 语义)。"""
    return (r.confidence == 0.0
            and bool(r.reasoning)
            and r.reasoning.lstrip().startswith("["))


def _detail_json(r: OpResult) -> str:
    return json.dumps({
        "reasoning": r.reasoning,
        "evidence": r.evidence or {},
        "tools_used": r.tools_used or [],
        "raw_score": r.raw_score,   # 未 clamp 的连续强度(排序用);旧记录无此键→None→退化 score
    }, ensure_ascii=False, default=str)


def get_operator_result(code: str, as_of, operator_id: str,
                        fingerprint: str) -> OpResult | None:
    """命中缓存 → 重建 OpResult;否则 None。weight 由 runner 汇总时按策略 YAML 赋,不入库。"""
    with session_scope() as s:
        row = s.exec(select(OperatorResult).where(
            OperatorResult.asset_code == code,
            OperatorResult.as_of == as_of,
            OperatorResult.operator_id == operator_id,
            OperatorResult.fingerprint == fingerprint,
        )).first()
    if row is None or not row.detail:
        return None
    try:
        d = json.loads(row.detail)
    except (json.JSONDecodeError, TypeError):
        return None
    return OpResult(
        operator=row.operator_id,
        type=row.operator_type,
        signal=row.signal or "hold",
        score=row.score if row.score is not None else 0.0,
        confidence=row.confidence if row.confidence is not None else 0.5,
        reasoning=d.get("reasoning", ""),
        evidence=d.get("evidence") or {},
        tools_used=d.get("tools_used") or [],
        target_weight=row.target_weight,
        value=row.value,
        veto=row.veto,
        raw_score=d.get("raw_score"),   # 旧记录无此键→None→聚合时退化为 score
    )


def save_operator_result(code: str, as_of, operator_id: str, fingerprint: str,
                         result: OpResult, op_type: str) -> bool:
    """upsert 一条算子缓存。失败结果不落库(返回 False),重跑时重试。"""
    if _is_failure(result):
        return False
    with session_scope() as s:
        row = s.exec(select(OperatorResult).where(
            OperatorResult.asset_code == code,
            OperatorResult.as_of == as_of,
            OperatorResult.operator_id == operator_id,
            OperatorResult.fingerprint == fingerprint,
        )).first()
        row = row or OperatorResult(asset_code=code, as_of=as_of,
                                    operator_id=operator_id, fingerprint=fingerprint)
        row.operator_type = op_type
        row.signal = result.signal
        row.score = result.score
        row.confidence = result.confidence
        row.veto = result.veto
        row.target_weight = result.target_weight
        row.value = result.value
        row.detail = _detail_json(result)
        row.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        if row.id is None:
            s.add(row)
        s.commit()
    return True


def count_operator_results(codes: list[str], start, end,
                           operator_ids: Iterable[str]) -> int:
    """[start,end] 内 codes 的指定算子集已落库行数(回测 progress 用)。

    语义:已覆盖的 (code, 交易日, 算子) 组合数,反映真实剩余计算量。
    active 策略的算子 id 由调用方从 CompiledStrategy.operators 取。
    fingerprints: {operator_id: fingerprint} 可选过滤,改了 prompt/params 后旧缓存不计入进度。
    """
    codes = codes or []
    oids = list(operator_ids or [])
    if not codes or not oids:
        return 0
    with session_scope() as s:
        rows = s.exec(select(OperatorResult.id).where(
            OperatorResult.asset_code.in_(codes),
            OperatorResult.as_of.between(start, end),
            OperatorResult.operator_id.in_(oids),
        )).all()
    if fingerprints:
        fp_set = set(fingerprints.values())
        with session_scope() as s:
            rows = s.exec(select(OperatorResult.id).where(
                OperatorResult.asset_code.in_(codes),
                OperatorResult.as_of.between(start, end),
                OperatorResult.operator_id.in_(oids),
                OperatorResult.fingerprint.in_(list(fp_set)),
            )).all()
    return len(rows)
