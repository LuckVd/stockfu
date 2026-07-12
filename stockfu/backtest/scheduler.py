"""回测 LLM 调度: temp=0 + ai_report 库表缓存(read-first)+ 进度 + 产物保存。

run(codes, start, end): 跑完整回测。analyze 结果按 (code, as_of) 缓存到 ai_report 表,
与实盘 run_ai_analysis 共用同一数据源——命中复用、未命中跑 LLM 落库。同区间重跑自动
跳过已入库的 LLM 调用(调仓序列每次重算,纯内存秒级)。改策略参数无需新 run_id(分析不依赖
策略参数,只 PositionManager 依赖)。产物存 data/backtest/{run_id}.json。
"""
from __future__ import annotations

import dataclasses
import gzip
import json
import os
from datetime import date, datetime

from stockfu.backtest import engine


def _data_dir() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data", "backtest"))


def _open_result(path: str):
    """按扩展名选择读取方式:.json.gz 走 gzip,.json 明文。向后兼容旧产物。"""
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, encoding="utf-8")


def _load_result(path: str) -> dict:
    with _open_result(path) as f:
        return json.load(f)


def _write_meta(run_id: str, result: dict, data_path: str) -> str:
    """写轻量摘要 {run_id}.meta.json(几 KB)。list_runs 只读它,避免对大产物全量解析。
    含 codes 全集(788 个字符串仅几 KB),保证 list 返回结构与回退解析路径一致。"""
    meta = {
        "schema_version": result.get("schema_version", 1),
        "run_id": run_id,
        "strategy_id": result.get("strategy_id"),
        "strategy_name": result.get("strategy_name"),
        "start": result.get("start"),
        "end": result.get("end"),
        "days": result.get("days"),
        "codes": result.get("codes", []),
        "operators": result.get("operators"),
        "metrics": result.get("metrics"),
        "data_file": os.path.basename(data_path),
        "data_size": os.path.getsize(data_path),
    }
    meta_path = os.path.join(_data_dir(), f"{run_id}.meta.json")
    tmp = f"{meta_path}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, default=str)
    os.replace(tmp, meta_path)  # 原子写
    return meta_path


def new_run_id() -> str:
    return datetime.now().strftime("run-%Y%m%d-%H%M%S")


def _make_cached_analyze(strategy=None, temperature: float = 0.0):
    """返回 analyze_fn。

    算子级缓存在 strategy.analyze 内部(read-through operator_result,math/llm 全局复用),
    这里仅注入 active 策略 + temp=0(回测确定性)。holding_override 透传(仓位层用,
    信号层已去持仓依赖,不影响算子输出/缓存指纹)。
    """
    if strategy is None:
        from stockfu.ai.operators.runner import get_active_strategy
        strategy = get_active_strategy()

    def _fn(code, as_of, holding_override):
        return strategy.analyze(code, as_of=as_of, holding_override=holding_override,
                                temperature=temperature)
    return _fn


def run(codes: list[str], start, end, initial_cash: float = engine.INITIAL_CASH,
        run_id: str | None = None, max_workers: int = 4,
        buy_cool_down_days: int | None = None, max_target_step: float | None = None,
        risk_confirm_days: int | None = None,
        target_mode: str | None = None, max_weight: float | None = None,
        total_dead: float | None = None, min_trade_weight: float | None = None,
        sell_cooldown_days: int | None = None, conf_gate: float | None = None) -> dict:
    """跑回测(active 策略驱动 analyze + debounce)。run_id 自动生成;分析缓存按 (code,as_of)
    进 ai_report 表,同区间重跑复用已入库行 = 断点续跑。返回结果并存盘。

    去抖参数默认 None=用 active 策略 YAML 的 debounce;显式传非 None 则覆盖(调试用)。
    active 策略由 get_active_strategy()(读 app_config('active_strategy_id'))决定。旧 is_active 列已移除。
    """
    from datetime import date as _date
    if isinstance(start, str):
        start = _date.fromisoformat(start)
    if isinstance(end, str):
        end = _date.fromisoformat(end)
    from stockfu.ai.operators.runner import get_active_strategy
    cs = get_active_strategy()
    run_id = run_id or new_run_id()
    analyze_fn = _make_cached_analyze(cs)
    # 基线=active 策略 YAML 的 StrategyDebounce;显式覆盖参数(非 None)替换对应字段
    db = cs.debounce_params
    overrides = {"buy_cool_down_days": buy_cool_down_days, "max_target_step": max_target_step,
                 "risk_confirm_days": risk_confirm_days, "target_mode": target_mode,
                 "max_weight": max_weight, "total_dead": total_dead,
                 "min_trade_weight": min_trade_weight, "sell_cooldown_days": sell_cooldown_days,
                 "conf_gate": conf_gate}
    db = dataclasses.replace(db, **{k: v for k, v in overrides.items() if v is not None})
    result = engine.run_backtest(codes, start, end, initial_cash,
                                 analyze_fn=analyze_fn, max_workers=max_workers, debounce=db)
    result["run_id"] = run_id
    # 落盘策略身份:engine 只拿到 analyze_fn 闭包、看不到策略,故在此补齐(cs 现成)。
    # 同时记算子 id 列表,产物可追溯(策略名 + 算子指纹)。
    result["strategy_id"] = cs.strategy_id
    result["strategy_name"] = cs.name
    result["operators"] = [op.get("id") for op in cs.operators if op.get("id")]
    os.makedirs(_data_dir(), exist_ok=True)
    out = os.path.join(_data_dir(), f"{run_id}.json.gz")
    # 落盘副本:标 schema 版本。trades 完整保留(含 pending 调仓意图——信号复盘需要);
    # 体积由 gzip + 摘要旁路(_write_meta)吸收,不再取舍。
    persist = dict(result)
    persist["schema_version"] = 1
    # 原子写:先写 .tmp 再 os.replace,避免中断/崩溃留下半截损坏文件
    # (list_runs 会静默吞掉损坏 JSON,表现为"回测凭空消失")。gzip 后体积约 1/6~1/10。
    tmp = f"{out}.tmp{os.getpid()}"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(persist, f, ensure_ascii=False, default=str)
    os.replace(tmp, out)
    _write_meta(run_id, persist, out)  # 轻量摘要:list_runs 只读 meta,不碰大文件
    result["saved_to"] = out
    return result


def list_runs() -> list[dict]:
    """列出已保存的回测 run 摘要(最新在前)。

    优先读轻量 .meta.json(几 KB),避免对每个大产物(数 MB gz)全量解析——没旁路时
    列 N 个回测是 O(N×filesize),几百 MB 解析会卡死前端。无 meta 的旧产物回退
    _load_result 全量解析(向后兼容)。按 mtime 倒序(最新在前)。
    """
    d = _data_dir()
    if not os.path.isdir(d):
        return []
    entries: list[tuple[float, dict]] = []  # (mtime, summary)
    seen: set[str] = set()

    # 1) 优先读 meta(每文件几 KB,不碰大产物)
    for fn in os.listdir(d):
        if not fn.endswith(".meta.json"):
            continue
        rid = fn[:-len(".meta.json")]
        mp = os.path.join(d, fn)
        try:
            m = _load_result(mp)
        except Exception:  # noqa: BLE001
            continue
        seen.add(rid)
        entries.append((os.path.getmtime(mp), {
            "run_id": m.get("run_id", rid), "start": m.get("start"),
            "end": m.get("end"), "codes": m.get("codes"),
            "strategy_id": m.get("strategy_id"),
            "strategy_name": m.get("strategy_name"),
            "metrics": m.get("metrics"),
            "data_file": m.get("data_file"), "data_size": m.get("data_size"),
        }))

    # 2) 无 meta 的旧产物:回退全量解析(仅扫 .json / .json.gz,跳过 .meta.json)
    for fn in os.listdir(d):
        if fn.endswith(".meta.json"):
            continue
        if fn.endswith(".json.gz"):
            rid = fn[:-len(".json.gz")]
        elif fn.endswith(".json"):
            rid = fn[:-len(".json")]
        else:
            continue
        if rid in seen:
            continue
        dp = os.path.join(d, fn)
        try:
            r = _load_result(dp)
        except Exception:  # noqa: BLE001
            continue
        entries.append((os.path.getmtime(dp), {
            "run_id": r.get("run_id", rid), "start": r.get("start"),
            "end": r.get("end"), "codes": r.get("codes"),
            "strategy_id": r.get("strategy_id"),
            "strategy_name": r.get("strategy_name"),
            "metrics": r.get("metrics"),
            "data_file": fn, "data_size": os.path.getsize(dp),
        }))

    entries.sort(key=lambda x: -x[0])
    return [e[1] for e in entries]


def load_run(run_id: str) -> dict | None:
    d = _data_dir()
    for ext in (".json.gz", ".json"):  # 优先读 gzip 新产物,回退明文旧产物
        p = os.path.join(d, f"{run_id}{ext}")
        if os.path.exists(p):
            return _load_result(p)
    return None


def progress(run_id: str, codes: list[str], start, end) -> dict:
    """查 operator_result 算进度(已覆盖 (code,���易日,算子) 数 / 总数)。供前端轮询。

    active 策略的算子集决定 total;run_id 仅为前端状态���留。算子级缓存下,每 (code,as_of)
    需算 len(算子) 个算子结果,已落库数 / 总数 = 进度。
    按当前 fingerprint 过滤统计:改了 prompt/params 后旧缓存不计入 done(进度更精确)。
    """
    from stockfu.ai.operator_cache import (compute_fingerprint,
                                           count_operator_results)
    from stockfu.ai.operators.runner import (_load_operator_meta,
                                             get_active_strategy)
    from stockfu.ai.operators.registry import get_operator_class
    cs = get_active_strategy()
    op_ids = [op["id"] for op in cs.operators if op.get("id")]
    days = engine._trade_calendar_days(start, end)
    total = len(days) * len(codes) * max(len(op_ids), 1)

    # 按当前 fingerprint 统计(避免改了 prompt/params 后旧缓存��计为 done)
    fp_map: dict[str, str] = {}
    for spec in cs.operators:
        oid = spec.get("id")
        if not oid:
            continue
        cls = get_operator_class(oid)
        if cls is None:
            continue
        params = dict(spec.get("params") or {})
        if cls.type == "llm":
            prompt, version = _load_operator_meta(oid)
            fp_map[oid] = compute_fingerprint(
                "llm", version=version, prompt=prompt,
                temperature=params.get("temperature", 0.0))
        elif cls.type == "math":
            _, version = _load_operator_meta(oid)
            fp_map[oid] = compute_fingerprint("math", version=version, params=params)
        # aggregator 不缓存,跳过
    done = count_operator_results(codes, start, end, op_ids, fingerprints=fp_map)
    return {"done": done, "total": total,
            "pct": round(done / total * 100, 1) if total else 0.0}
