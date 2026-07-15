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
    """LLM 行的 detail JSON(math 行不写 detail=None)。raw_score 已提独立列,不进 JSON。"""
    return json.dumps({
        "reasoning": r.reasoning,
        "evidence": r.evidence or {},
        "tools_used": r.tools_used or [],
    }, ensure_ascii=False, default=str)


def _row_to_opresult(row: "OperatorResult") -> OpResult:
    """从 DB 行重建 OpResult(单点读 get_operator_result 与批量读 get_operator_results_batch
    共用,保证字段映射逐字一致 —— 行为漂移会改回测结果)。

    raw_score 优先取独立列(全精度,冷热一致);列为 NULL(回填前的旧行)回退 detail JSON。
    detail 为空(math 行)时 reasoning/evidence/tools_used 取默认值(math 回测不用)。
    """
    d: dict = {}
    if row.detail:
        try:
            d = json.loads(row.detail)
        except (json.JSONDecodeError, TypeError):
            d = {}
    raw = row.raw_score
    if raw is None:
        raw = d.get("raw_score")  # 回填前旧行:raw_score 仍在 detail 里
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
        raw_score=raw,
    )


def get_operator_result(code: str, as_of, operator_id: str,
                        fingerprint: str) -> OpResult | None:
    """命中缓存 → 重建 OpResult;否则 None。weight 由 runner 汇总时按策略 YAML 赋,不入库。

    行存在即命中(核心数据在独立列;math 行 detail=NULL 也算命中)。"""
    with session_scope() as s:
        row = s.exec(select(OperatorResult).where(
            OperatorResult.asset_code == code,
            OperatorResult.as_of == as_of,
            OperatorResult.operator_id == operator_id,
            OperatorResult.fingerprint == fingerprint,
        )).first()
    if row is None:
        return None
    return _row_to_opresult(row)


def get_operator_results_batch(
    codes: list[str], as_of, op_fps: list[tuple[str, str]]
) -> dict[tuple[str, str], OpResult]:
    """单日批量读缓存:一次 SELECT 取回 (codes × as_of × 算子集) 全部命中行。

    替代回测里每个 (code,as_of,算子) 一次 get_operator_result 的 N×M 次往返 ——
    engine Phase 2 提交线程池前调一次,把 dict 注入各 analyze 作预填缓存。

    op_fps: [(operator_id, fingerprint), ...] 策略叶子算子的 (id, 指纹) 对
      (指纹不依赖 code/as_of,策略编译时算一次即可,见 CompiledStrategy._op_meta)。
    返回 {(asset_code, operator_id): OpResult};miss 的组合不在 dict(调用方落 miss 计算+upsert)。
    按 (operator_id, fingerprint) 对精确过滤,防指纹跨算子碰撞误命中。
    """
    codes = codes or []
    op_fps = op_fps or []
    if not codes or not op_fps:
        return {}
    op_ids = [oid for oid, _ in op_fps]
    fps = [fp for _, fp in op_fps]
    valid_pairs = set(op_fps)
    out: dict[tuple[str, str], OpResult] = {}
    with session_scope() as s:
        rows = s.exec(select(OperatorResult).where(
            OperatorResult.as_of == as_of,
            OperatorResult.asset_code.in_(codes),
            OperatorResult.operator_id.in_(op_ids),
            OperatorResult.fingerprint.in_(fps),
        )).all()
    for row in rows:
        if (row.operator_id, row.fingerprint) not in valid_pairs:
            continue
        out[(row.asset_code, row.operator_id)] = _row_to_opresult(row)
    return out


def save_operator_result(code: str, as_of, operator_id: str, fingerprint: str,
                         result: OpResult, op_type: str) -> bool:
    """upsert 一条算子缓存。失败结果不落库(返回 False),重跑时重试。

    raw_score 写独立列(全精度);math 行 detail=NULL(回测不读 reasoning/evidence/tools_used,
    省 JSON 序列化+存储),LLM 行 detail 存 reasoning/evidence/tools_used(昂贵 LLM 产物)。
    """
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
        row.raw_score = result.raw_score
        row.confidence = result.confidence
        row.veto = result.veto
        row.target_weight = result.target_weight
        row.value = result.value
        row.detail = None if op_type == "math" else _detail_json(result)
        row.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        if row.id is None:
            s.add(row)
        s.commit()
    return True
