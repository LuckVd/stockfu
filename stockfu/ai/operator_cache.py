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

from contextlib import contextmanager

from sqlalchemy import event, text
from sqlmodel import Session, create_engine, select

from stockfu.ai.operators.base import OpResult
from stockfu.config import settings
from stockfu.models import OperatorResult

cache_engine = create_engine(settings.operator_cache_db_url, echo=False,
                             connect_args={"check_same_thread": False})


@event.listens_for(cache_engine, "connect")
def _cache_pragmas(conn, _record):
    cur = conn.cursor(); cur.execute("PRAGMA busy_timeout=5000"); cur.execute("PRAGMA journal_mode=WAL"); cur.close()


def _ensure_cache_table() -> None:
    OperatorResult.__table__.create(cache_engine, checkfirst=True)


@contextmanager
def cache_session_scope():
    _ensure_cache_table()
    with Session(cache_engine) as s:
        yield s


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
    with cache_session_scope() as s:
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
    with cache_session_scope() as s:
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


# ---- 紧凑区间预载(E):回测启动一次 SQL,日循环内存 hit,省每日 prefetch 扫库 ----
# pack = (signal, score, confidence, value, veto); unpack 成 OpResult 语义与 _row_to_opresult 一致。

def pack_opresult(r: OpResult) -> tuple:
    """OpResult → 定长 tuple(省 dict/对象头;区间预载百万级用)。"""
    return (
        r.signal or "hold",
        float(r.score) if r.score is not None else 0.0,
        float(r.confidence) if r.confidence is not None else 0.5,
        r.value,
        bool(r.veto),
    )


def unpack_opresult(operator_id: str, op_type: str, packed: tuple) -> OpResult:
    """pack_opresult 逆操作;字段默认与 _row_to_opresult / math 行一致。"""
    signal, score, confidence, value, veto = packed
    return OpResult(
        operator=operator_id,
        type=op_type or "math",
        signal=signal or "hold",
        score=score if score is not None else 0.0,
        confidence=confidence if confidence is not None else 0.5,
        value=value,
        veto=bool(veto),
    )


def load_operator_results_range(
    codes: list[str], start, end, op_fps: list[tuple[str, str]],
    op_types: dict[str, str] | None = None,
) -> dict:
    """区间批量预载算子缓存 → 紧凑结构 {as_of: {op_id: {code: pack_tuple}}}。

    **raw SQL + 仅必要列 + 流式 fetchmany**,不经 ORM(全量 ORM 在 3.6G 机可顶到 3GB+)。
    pack=(signal, score, confidence, value, veto)。op_types 仅 begin_run 侧保留供 unpack。
    """
    from datetime import date as _date

    from sqlalchemy import text

    _ensure_cache_table()

    codes = list(codes or [])
    op_fps = list(op_fps or [])
    if not codes or not op_fps or start is None or end is None:
        return {}
    op_ids = list({oid for oid, _ in op_fps})
    fps = list({fp for _, fp in op_fps})
    valid_pairs = set(op_fps)
    # 嵌套 dict 比 (code,op_id) 元组键更省小对象
    out: dict = {}  # date -> {op_id: {code: pack}}

    # 分块 IN,避免超长 SQL;每块 raw 流式读
    def _chunks(xs, n=400):
        for i in range(0, len(xs), n):
            yield xs[i:i + n]

    start_s = start.isoformat() if hasattr(start, "isoformat") else str(start)
    end_s = end.isoformat() if hasattr(end, "isoformat") else str(end)
    op_ph = ", ".join(f":op{i}" for i in range(len(op_ids)))
    fp_ph = ", ".join(f":fp{i}" for i in range(len(fps)))
    base_params = {f"op{i}": v for i, v in enumerate(op_ids)}
    base_params.update({f"fp{i}": v for i, v in enumerate(fps)})
    base_params["start"] = start_s
    base_params["end"] = end_s

    with cache_engine.connect() as conn:
        for code_chunk in _chunks(codes, 400):
            c_ph = ", ".join(f":c{i}" for i in range(len(code_chunk)))
            params = dict(base_params)
            params.update({f"c{i}": v for i, v in enumerate(code_chunk)})
            sql = text(
                f"SELECT asset_code, as_of, operator_id, fingerprint, "
                f"signal, score, confidence, value, veto "
                f"FROM operator_result "
                f"WHERE as_of >= :start AND as_of <= :end "
                f"AND operator_id IN ({op_ph}) "
                f"AND fingerprint IN ({fp_ph}) "
                f"AND asset_code IN ({c_ph})"
            )
            result = conn.execute(sql, params)
            while True:
                rows = result.fetchmany(5000)
                if not rows:
                    break
                for asset_code, as_of, operator_id, fingerprint, signal, score, confidence, value, veto in rows:
                    if (operator_id, fingerprint) not in valid_pairs:
                        continue
                    if isinstance(as_of, str):
                        as_of = _date.fromisoformat(as_of[:10])
                    packed = (
                        signal or "hold",
                        float(score) if score is not None else 0.0,
                        float(confidence) if confidence is not None else 0.5,
                        float(value) if value is not None else None,
                        bool(veto),
                    )
                    by_op = out.get(as_of)
                    if by_op is None:
                        by_op = {}
                        out[as_of] = by_op
                    by_code = by_op.get(operator_id)
                    if by_code is None:
                        by_code = {}
                        by_op[operator_id] = by_code
                    by_code[asset_code] = packed
    return out


def prefill_from_run_cache(
    run_cache: dict, as_of, codes: list[str], op_fps: list[tuple[str, str]],
    op_types: dict[str, str],
) -> dict[tuple[str, str], OpResult]:
    """从紧凑区间缓存拆出单日 prefill {(code, op_id): OpResult}。

    run_cache 结构: {as_of: {op_id: {code: pack}}}。
    """
    day = (run_cache or {}).get(as_of) or {}
    if not day:
        return {}
    out: dict[tuple[str, str], OpResult] = {}
    for op_id, _fp in op_fps:
        by_code = day.get(op_id) or {}
        if not by_code:
            continue
        otype = op_types.get(op_id, "math")
        for c in codes:
            packed = by_code.get(c)
            if packed is None:
                continue
            out[(c, op_id)] = unpack_opresult(op_id, otype, packed)
    return out


def put_run_cache_day(
    run_cache: dict, as_of, code_results: dict, op_id: str,
) -> None:
    """把新算的 {code: OpResult} 写回紧凑区间缓存(与 DB batch save 同步)。"""
    if not code_results:
        return
    day = run_cache.setdefault(as_of, {})
    by_code = day.setdefault(op_id, {})
    for c, r in code_results.items():
        by_code[c] = pack_opresult(r)


def save_operator_result(code: str, as_of, operator_id: str, fingerprint: str,
                         result: OpResult, op_type: str) -> bool:
    """upsert 一条算子缓存。失败结果不落库(返回 False),重跑时重试。

    math 行 detail=NULL(LLM 已下线;回测不读 reasoning,省 JSON 序列化+存储)。
    """
    if _is_failure(result):
        return False
    with cache_session_scope() as s:
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
    entries = [
        (c, operator_id, fingerprint, op_type, r)
        for c, r in code_results.items()
    ]
    return save_operator_results_batch(as_of, entries)


def save_operator_results_batch(as_of, entries: list) -> int:
    """单日多算子批量 upsert:一次 session、一次 commit。

    entries: [(code, operator_id, fingerprint, op_type, OpResult), ...]
    回测冷启动把当日全部 miss 算子攒齐后调用(800 票 × 3 算子 → 1 次 commit,
    取代逐 (code,as_of,算子) 的 save_operator_result)。字段映射与
    save_operator_result / save_operator_results_day 逐字一致。
    返回实际落库行数(失败结果 _is_failure 跳过)。
    """
    valid: list[tuple] = []
    for item in entries or []:
        code, operator_id, fingerprint, op_type, result = item
        if _is_failure(result):
            continue
        valid.append((code, operator_id, fingerprint, op_type, result))
    if not valid:
        return 0
    codes = list({e[0] for e in valid})
    op_ids = list({e[1] for e in valid})
    fps = list({e[2] for e in valid})
    with cache_session_scope() as s:
        existing = {
            (r.asset_code, r.operator_id, r.fingerprint): r
            for r in s.exec(select(OperatorResult).where(
                OperatorResult.as_of == as_of,
                OperatorResult.asset_code.in_(codes),
                OperatorResult.operator_id.in_(op_ids),
                OperatorResult.fingerprint.in_(fps),
            )).all()
        }
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        for code, operator_id, fingerprint, op_type, result in valid:
            key = (code, operator_id, fingerprint)
            row = existing.get(key) or OperatorResult(
                asset_code=code, as_of=as_of,
                operator_id=operator_id, fingerprint=fingerprint,
            )
            row.operator_type = op_type
            row.signal = result.signal
            row.score = result.score
            row.confidence = result.confidence
            row.veto = result.veto
            row.target_weight = result.target_weight
            row.value = result.value
            row.detail = None
            row.updated_at = stamp
            if row.id is None:
                s.add(row)
                existing[key] = row  # 防 entries 内同 key 重复 add
        s.commit()
    return len(valid)


def clear_operator_cache(operator_id: str | None = None) -> int:
    _ensure_cache_table()
    with cache_engine.begin() as conn:
        sql = "DELETE FROM operator_result"
        params = {}
        if operator_id:
            sql += " WHERE operator_id = :operator_id"; params["operator_id"] = operator_id
        return int(conn.execute(text(sql), params).rowcount or 0)
