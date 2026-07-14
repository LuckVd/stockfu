"""策略编排: YAML → CompiledStrategy → analyze_fn(注入 engine,零改动)。

CompiledStrategy.analyze(code, as_of, holding_override, temperature) 签名与返回结构
{context, opinions, aggregate, narrative} 完全等同 ai.analyze,可直接当 analyze_fn
传给 engine.run_backtest / scheduler._make_cached_analyze。

aggregate dict 契约(= synthesis.aggregate 返回,engine.py Phase3 直接消费):
  {final_signal, total_score, risk_vetoed, ai_target_weight, confidence}

debounce_kwargs 属性把 YAML 的 position+debounce 翻译成 engine.run_backtest 的
去抖形参(target_mode/max_weight/...),scheduler 透传——保留全部 C0-C6 去抖成果。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import yaml
from sqlmodel import select

from stockfu.ai.operators.base import OpContext
from stockfu.ai.operators.registry import REGISTRY, discover_and_register, get_operator_class


@dataclass(frozen=True)
class StrategyDebounce:
    """策略去抖 + 仓位参数(类型安全契约,取代 runner↔engine 的字符串 dict 耦合)。

    to_dict() 产出与旧 debounce_kwargs 完全相同的 9-key dict,保 engine 形参 + 前端 metrics[config]。
    """
    buy_cool_down_days: int = 5
    max_target_step: float = 1.0
    risk_confirm_days: int = 1
    min_trade_weight: float = 0.0
    sell_cooldown_days: int = 0
    conf_gate: float = 0.0
    target_mode: str = "discrete"
    max_weight: float = 0.15
    total_dead: float = 3.0
    targets: dict | None = None  # 信号→仓位映射表(YAML position.targets传入,None=用框架默认)

    def to_dict(self) -> dict:
        return {
            "buy_cool_down_days": self.buy_cool_down_days,
            "max_target_step": self.max_target_step,
            "risk_confirm_days": self.risk_confirm_days,
            "min_trade_weight": self.min_trade_weight,
            "sell_cooldown_days": self.sell_cooldown_days,
            "conf_gate": self.conf_gate,
            "target_mode": self.target_mode,
            "max_weight": self.max_weight,
            "total_dead": self.total_dead,
            "targets": self.targets or {},
        }


@dataclass
class CompiledStrategy:
    """已编译的策略:持有算子规格 + 汇总/仓位/去抖配置,analyze() 产出 engine 期望结构。"""
    strategy_id: str = ""
    name: str = ""
    operators: list[dict] = field(default_factory=list)
    aggregate: dict = field(default_factory=dict)
    position: dict = field(default_factory=dict)
    debounce: dict = field(default_factory=dict)

    # ---- engine 注入接口 ----
    def analyze(self, code: str, as_of=None, holding_override=None,
                temperature: float = 0.2) -> dict:
        from stockfu.ai.context import build_context
        from stockfu.ai.synthesis import narrate

        has_llm = any(op.get("type") == "llm" for op in self.operators)
        advisor_ctx = None
        ctx_name = ""
        if has_llm:
            advisor_ctx = build_context(code, as_of=as_of)
            ctx_name = advisor_ctx.name
        ctx = OpContext(code=code, name=ctx_name, as_of=as_of,
                        advisor_ctx=advisor_ctx)

        # 1. 跑所有叶子算子(math 先跑,结果进 ctx.factors 供 llm 算子参考)。
        #    算子级缓存:math/llm read-through(operator_result),aggregator 不缓存。
        from stockfu.ai.operator_cache import (compute_fingerprint,
                                               get_operator_result,
                                               save_operator_result)
        results = []
        for spec in self.operators:
            cls = get_operator_class(spec["id"])
            if cls is None:
                raise ValueError(f"未知算子 '{spec['id']}'(策略 {self.strategy_id})")
            params = dict(spec.get("params") or {})
            if has_llm:
                params["temperature"] = temperature

            # 算指纹→命中复用/miss→run+save(aggregator 跳过,纯函数重算廉价)
            r = None
            fp = None
            prompt = None
            if cls.type == "llm":
                prompt, version = _load_operator_meta(cls.operator_id)
                fp = compute_fingerprint("llm", version=version, prompt=prompt,
                                         temperature=params.get("temperature"))
                r = get_operator_result(code, as_of, spec["id"], fp)
            elif cls.type == "math":
                _, version = _load_operator_meta(cls.operator_id)
                fp = compute_fingerprint("math", version=version, params=params)
                r = get_operator_result(code, as_of, spec["id"], fp)

            if r is None:
                inst = cls(prompt=prompt) if cls.type == "llm" else cls()
                r = inst.run(ctx, params)
                if fp is not None:
                    save_operator_result(code, as_of, spec["id"], fp, r, cls.type)

            r.weight = float(spec.get("weight", 1.0))
            if spec.get("veto_role") and r.signal in ("sell", "strong_sell"):
                r.veto = True   # 显式 veto_role 标记兜底(risk 算子自身也设)
            results.append(r)
            if cls.type == "math" and r.value is not None:
                ctx.factors[spec["id"]] = r.value

        # 2. 汇总
        method = self.aggregate.get("method", "weighted_sum")
        agg_cls = get_operator_class(method)
        if agg_cls is None or agg_cls.type != "aggregator":
            raise ValueError(f"未知/非汇总算子 '{method}'")
        summary = agg_cls().aggregate(results, {"thresholds": self.aggregate.get("thresholds")})

        # 3. 组装 engine 期望的 aggregate dict(契约 = synthesis.aggregate 返回)
        opinions = [{
            "advisor": r.operator, "signal": r.signal, "score": r.score,
            "confidence": r.confidence, "reasoning": r.reasoning,
            "tools_used": [], "target_weight": r.target_weight,
        } for r in results]
        aggregate = {
            "final_signal": summary.signal,
            "total_score": summary.score,
            "total_raw": summary.raw_score if summary.raw_score is not None else summary.score,
            "risk_vetoed": summary.veto,
            "ai_target_weight": summary.target_weight,
            "confidence": summary.confidence,
        }

        # 4. narrative: 有 LLM 时调 narrate(等价现状);纯数学规则拼接(不调 LLM,秒级)
        if has_llm:
            try:
                narrative = narrate({**aggregate, "opinions": opinions})
            except Exception as exc:  # noqa: BLE001
                narrative = f"[综合解读失败] {exc}"
        else:
            parts = "; ".join(f"{r.operator}={r.signal}({r.score:+.1f})" for r in results)
            narrative = f"[{self.name}] {summary.signal} | total={summary.score} | {parts}"

        return {
            "code": code, "name": ctx_name,
            "context": advisor_ctx.__dict__ if advisor_ctx else {},
            "opinions": opinions,
            "aggregate": aggregate,
            "narrative": narrative,
        }

    @property
    def debounce_params(self) -> StrategyDebounce:
        """把 position+debounce YAML 段编译成 StrategyDebounce(类型安全)。"""
        d = self.debounce or {}
        p = self.position or {}
        return StrategyDebounce(
            buy_cool_down_days=d.get("buy_cool_down_days", 5),
            max_target_step=d.get("max_target_step", 1.0),
            risk_confirm_days=d.get("risk_confirm_days", 1),
            min_trade_weight=d.get("min_trade_weight", 0.0),
            sell_cooldown_days=d.get("sell_cooldown_days", 0),
            conf_gate=d.get("conf_gate", 0.0),
            target_mode=p.get("mode", "discrete"),
            max_weight=p.get("max_w", 0.15),
            total_dead=p.get("dead", 3.0),
            targets=p.get("targets"),
        )

    @property
    def debounce_kwargs(self) -> dict:
        """旧接口(兼容):返回 to_dict()。新代码用 .debounce_params 拿 dataclass。"""
        return self.debounce_params.to_dict()


def _load_operator_meta(operator_id: str) -> tuple[str | None, int]:
    """从 operator 表读 LLM 算子 prompt + version。prompt=None→算子用 advisor.system_prompt() 兜底。"""
    from stockfu.db import session_scope
    from stockfu.models import Operator
    with session_scope() as s:
        row = s.get(Operator, operator_id)
        if row:
            return row.prompt, row.version
        return None, 1


def load_yaml(path: str) -> "CompiledStrategy":
    """从 YAML 文件加载编译策略(供 strategies/*.yaml 模板导入)。"""
    with open(path, encoding="utf-8") as f:
        return compile_strategy(f.read())


def compile_strategy(yaml_text: str) -> CompiledStrategy:
    """YAML 文本 → CompiledStrategy。校验算子 id 与汇总方法存在性。"""
    if not REGISTRY:
        discover_and_register()
    cfg = yaml.safe_load(yaml_text) or {}
    for spec in cfg.get("operators", []):
        if not get_operator_class(spec["id"]):
            raise ValueError(f"未知算子 '{spec['id']}'(策略 {cfg.get('name', '')})")
    method = cfg.get("aggregate", {}).get("method", "weighted_sum")
    if not get_operator_class(method):
        raise ValueError(f"未知汇总算子 '{method}'")
    return CompiledStrategy(
        name=cfg.get("name", ""),
        operators=cfg.get("operators", []),
        aggregate=cfg.get("aggregate", {}),
        position=cfg.get("position", {}),
        debounce=cfg.get("debounce", {}),
    )


def get_active_strategy() -> CompiledStrategy:
    """读 app_config('active_strategy_id') 指针指向的 strategy 编译;无则兜底 classic_4advisors。

    active 由单一 app_config key 决定(物理唯一,取代 is_active 多选风险)。
    切策略:set_app_config('active_strategy_id', sid)。
    """
    from stockfu.db import get_app_config, session_scope
    from stockfu.models import Strategy
    if not REGISTRY:
        discover_and_register()
    with session_scope() as s:
        sid = get_app_config("active_strategy_id", "classic_4advisors")
        row = s.get(Strategy, sid)
        if row is None:
            row = s.get(Strategy, "classic_4advisors")
        if row is None:
            raise RuntimeError("无可用策略(operator/strategy 表未 seed?)")
        cs = compile_strategy(row.config)
        cs.strategy_id = row.strategy_id
        cs.name = cs.name or row.name
        return cs
