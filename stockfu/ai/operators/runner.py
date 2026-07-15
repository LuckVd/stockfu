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

import functools
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
    # 资金分配/风控(可选,YAML risk 段配;None=未配,用 engine 默认)
    max_gross: float | None = None
    stop_loss_pct: float | None = None
    portfolio_brake_dd: float | None = None

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
            "max_gross": self.max_gross,
            "stop_loss_pct": self.stop_loss_pct,
            "portfolio_brake_dd": self.portfolio_brake_dd,
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
    risk: dict = field(default_factory=dict)

    # ---- engine 注入接口 ----
    def _ensure_op_meta(self, temperature: float):
        """算子元信息预算(每策略+temperature 算一次,缓存于实例):
        [(spec, cls, fp, prompt, version)]。

        复刻 analyze 内的指纹计算(has_llm 时 params 注入 temperature),保证批量预读
        prefetch_cache 的 (op_id, fp) 键与单点读 get_operator_result 完全一致。
        干掉原本每 (code,as_of,算子) 一次的 _load_operator_meta + compute_fingerprint
        DB 往返(Operator.version 实际恒 0,版本由 prompt 驱动;此处只读一次)。"""
        cache = getattr(self, "_op_meta_cache", None)
        if cache is None:
            cache = {}
            self._op_meta_cache = cache  # type: ignore[attr-defined]
        if temperature in cache:
            return cache[temperature]
        from stockfu.ai.operator_cache import compute_fingerprint
        has_llm = any(op.get("type") == "llm" for op in self.operators)
        meta = []
        for spec in self.operators:
            cls = get_operator_class(spec["id"])
            if cls is None:
                raise ValueError(f"未知算子 '{spec['id']}'(策略 {self.strategy_id})")
            params = dict(spec.get("params") or {})
            if has_llm:
                params["temperature"] = temperature
            prompt = None
            if cls.type == "llm":
                prompt, version = _load_operator_meta(cls.operator_id)
                fp = compute_fingerprint("llm", version=version, prompt=prompt,
                                         temperature=params.get("temperature"))
            elif cls.type == "math":
                _, version = _load_operator_meta(cls.operator_id)
                fp = compute_fingerprint("math", version=version, params=params)
            else:  # aggregator(不应在 self.operators,保险)
                fp = None
            meta.append((spec, cls, fp, prompt, version))
        cache[temperature] = meta
        return meta

    def prefetch_cache(self, codes: list[str], as_of,
                       temperature: float = 0.0) -> dict:
        """单日批量预读缓存(回测 engine Phase 2 前调一次,主线程):
        一次 SELECT 取回 (codes × as_of × 算子集) 全部命中 → {(code, op_id): OpResult}。
        命中预填注入各 analyze,跳过逐 (code,as_of,算子) 的 get_operator_result 往返。"""
        from stockfu.ai.operator_cache import get_operator_results_batch
        meta = self._ensure_op_meta(temperature)
        op_fps = [(spec["id"], fp) for spec, cls, fp, _, _ in meta if fp is not None]
        return get_operator_results_batch(codes, as_of, op_fps)

    def analyze(self, code: str, as_of=None, holding_override=None,
                temperature: float = 0.2, cache_prefill: dict | None = None) -> dict:
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
        #    算子元信息(指纹/prompt/version)预算一次(_ensure_op_meta);cache_prefill
        #    命中则跳过 get_operator_result 往返,miss 仍落单行计算+upsert。
        from stockfu.ai.operator_cache import (get_operator_result,
                                               save_operator_result)
        meta = self._ensure_op_meta(temperature)
        results = []
        for spec, cls, fp, prompt, version in meta:
            params = dict(spec.get("params") or {})
            if has_llm:
                params["temperature"] = temperature

            # 命中复用:预填 → 单点读;miss → run+save(aggregator fp=None 跳过缓存)
            r = None
            if cache_prefill is not None and fp is not None:
                r = cache_prefill.get((code, spec["id"]))
            if r is None and fp is not None:
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
        """把 position+debounce+risk YAML 段编译成 StrategyDebounce(类型安全)。"""
        d = self.debounce or {}
        p = self.position or {}
        rk = self.risk or {}
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
            max_gross=rk.get("max_gross"),
            stop_loss_pct=rk.get("stop_loss"),
            portfolio_brake_dd=rk.get("portfolio_brake"),
        )

    @property
    def debounce_kwargs(self) -> dict:
        """旧接口(兼容):返回 to_dict()。新代码用 .debounce_params 拿 dataclass。"""
        return self.debounce_params.to_dict()


@functools.lru_cache(maxsize=None)
def _load_operator_meta(operator_id: str) -> tuple[str | None, int]:
    """从 operator 表读 LLM 算子 prompt + version。prompt=None→算子用 advisor.system_prompt() 兜底。

    进程级缓存(G09):operator 表仅 14 行、seed 期低频变更,同进程内每个 operator_id
    只查 1 次 DB。调用点仅 _ensure_op_meta(已实例级缓存),此 lru_cache 让同进程内
    第二个 CompiledStrategy 实例(如 API 连跑多次回测)也 0 session 开闭。
    失效:改 operator 表 prompt/version 后重启进程即清空(fingerprint 含 version,
    跨次回测自动失效);低频运维动作,不加手动清缓存 hook。
    """
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
        risk=cfg.get("risk", {}),
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
