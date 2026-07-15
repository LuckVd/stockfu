"""算子级回测缓存(operator_result 表)的读写 + 指纹计算。

去持仓依赖后,math 算子是纯市场数据函数 f(code, as_of, params),
同输入全局任意复用(跨策略/跨回测)。本模块提供 read-through 缓存的存取原语,
由 runner.CompiledStrategy.analyze 在算子循环里调用。

指纹(fingerprint)编码“影响算子输出的非(code,as_of)输入”:
  math: hash(version + params + source) —— params=窗口/周期;source=算子类源码 hash
    (改算子代码自动失效旧缓存,治 P2-5;不再依赖人工 bump version)
回测侧 LLM 已下线(实盘 AI 4 顾问走 ai/skills,不经此缓存)。
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


def compute_fingerprint(*, version: int = 1, params: dict | None = None,
                        source: str | None = None) -> str:
    """算子输入指纹(16 位 sha1)= hash(version + params + source)。仅 math 算子用
    (aggregator 不缓存;回测侧 LLM 已下线)。

    source = 算子类源码 hash(sha1(inspect.getsource(cls))[:8],由 _ensure_op_meta 算传入)。
    纳入源码 → 改算子 Python 逻辑(公式/clamp)自动失效旧缓存(治 P2-5),
    不再依赖人工 bump Operator.version(该字段降级为强制失效开关)。
    code/as_of 不进(已在表 key 列);holding 不进(去持仓)。
    """
    spec = {"version": version, "params": params or {}, "source": source}
    raw = json.dumps(spec, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _is_failure(r: OpResult) -> bool:
    """失败结果(置信 0 + reasoning 以方括号开头)不落库,重跑时重试(沿用原 classic 语义)。"""
    return (r.confidence == 0.0
            and bool(r.reasoning)
            and r.reasoning.lstrip().startswith("["))


def _row_to_opresult(row: "OperatorResult") -> OpResult:
    """从 DB 行重建 OpResult(单点读与批量读共用,字段映射逐字一致——漂移会改回测结果)。

    math 行 detail=None(LLM 已下线,不再写 detail);旧行 detail 里 reasoning 仍可读。
    """
    d: dict = {}
    if row.detail:
        try:
            d = json.loads(row.detail)
        except (json.JSONDecodeError, TypeError):
            d = {}
    return OpResult(
        operator=row.operator_id,
        type=row.operator_type,
        signal=row.signal or "hold",
        score=row.score if row.score is not None else 0.0,
        confidence=row.confidence if row.confidence is not None else 0.5,
        reasoning=d.get("reasoning", ""),
        target_weight=row.target_weight,
        value=row.value,
        veto=row.veto,
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

    math 行 detail=NULL(LLM 已下线;回测不读 reasoning,省 JSON 序列化+存储)。
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
        row.confidence = result.confidence
        row.veto = result.veto
        row.target_weight = result.target_weight
        row.value = result.value
        row.detail = None
        row.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        if row.id is None:
            s.add(row)
        s.commit()
    return True


def save_operator_results_day(code_results: dict, as_of, operator_id: str,
                              fingerprint: str, op_type: str) -> int:
    """批量 upsert 单日多 code 的算子缓存(一次 session,一次 commit)。

    code_results: {code: OpResult}(同一天的多个 code)。factor_diag 每日按 code 维度并发
    算 miss → 攒齐后一次落库,治"N 票 × N 日逐行 session 开闭"的首跑慢(800 票单日
    800 次 commit → 1 次)。指纹/字段映射与 save_operator_result 逐字一致 → 缓存互通。
    返回实际落库行数(失败结果 _is_failure 跳过)。
    """
    valid = {c: r for c, r in code_results.items() if not _is_failure(r)}
    if not valid:
        return 0
    with session_scope() as s:
        existing = {r.asset_code: r for r in s.exec(select(OperatorResult).where(
            OperatorResult.as_of == as_of,
            OperatorResult.operator_id == operator_id,
            OperatorResult.fingerprint == fingerprint,
            OperatorResult.asset_code.in_(list(valid.keys())),
        )).all()}
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        for c, r in valid.items():
            row = existing.get(c) or OperatorResult(asset_code=c, as_of=as_of,
                                                    operator_id=operator_id,
                                                    fingerprint=fingerprint)
            row.operator_type = op_type
            row.signal = r.signal
            row.score = r.score
            row.confidence = r.confidence
            row.veto = r.veto
            row.target_weight = r.target_weight
            row.value = r.value
            row.detail = None
            row.updated_at = stamp
            if row.id is None:
                s.add(row)
        s.commit()
    return len(valid)
