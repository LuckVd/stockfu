"""回测 LLM 调度: temp=0 + ai_report 库表缓存(read-first)+ 进度 + 产物保存。

run(codes, start, end): 跑完整回测。analyze 结果按 (code, as_of) 缓存到 ai_report 表,
与实盘 run_ai_analysis 共用同一数据源——命中复用、未命中跑 LLM 落库。同区间重跑自动
跳过已入库的 LLM 调用(调仓序列每次重算,纯内存秒级)。改策略参数无需新 run_id(分析不依赖
策略参数,只 PositionManager 依赖)。产物存 data/backtest/{run_id}.json。
"""
from __future__ import annotations

import dataclasses
import json
import os
from datetime import date, datetime

from stockfu.backtest import engine


def _data_dir() -> str:
    return os.path.join(os.path.dirname(__file__), "..", "..", "data", "backtest")


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
    os.makedirs(_data_dir(), exist_ok=True)
    out = os.path.join(_data_dir(), f"{run_id}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, default=str)
    result["saved_to"] = out
    return result


def list_runs() -> list[dict]:
    """列出已保存的回测 run 摘要(最新在前)。"""
    d = _data_dir()
    if not os.path.isdir(d):
        return []
    out = []
    for fn in sorted(os.listdir(d), reverse=True):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, fn), encoding="utf-8") as f:
                r = json.load(f)
            out.append({"run_id": r.get("run_id", fn[:-5]), "start": r.get("start"),
                        "end": r.get("end"), "codes": r.get("codes"),
                        "metrics": r.get("metrics")})
        except Exception:  # noqa: BLE001
            pass
    return out


def load_run(run_id: str) -> dict | None:
    p = os.path.join(_data_dir(), f"{run_id}.json")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


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
