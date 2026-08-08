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
import hashlib
import inspect
import os
from dataclasses import dataclass, field
from datetime import date as _date
from datetime import timedelta

import yaml

from stockfu.ai.operators.base import OpContext
from stockfu.ai.operators.registry import REGISTRY, discover_and_register, get_operator_class

# 回测算子缓存滚动预载:begin_run_cache 只预载未来 RUN_CACHE_WINDOW_DAYS 日历日窗口,
# prefetch_cache 消费到尾部提前量内再同步补下一块。长区间(如 2007-2026)峰值内存从
# "全区间 11.9M 行(~3.6G)" 降到 "窗口×每日行数"(250 日 ≈ 0.7M/日 ≈ 180M),已消费日
# 仍由 prefetch_cache 的 run_cache.pop(as_of) 逐日释放。窗口大小只影响分块加载频率
# (每块 = 3 条走索引 SQL)与峰值内存,对 3.7G 预算都可忽略,取大(≈1 年)减查询次数。
RUN_CACHE_WINDOW_DAYS = int(os.environ.get("STOCKFU_RUN_CACHE_WINDOW_DAYS", "250"))
RUN_CACHE_LOOKAHEAD_DAYS = int(os.environ.get("STOCKFU_RUN_CACHE_LOOKAHEAD_DAYS", "20"))


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
    score_full: float = 20.0  # 满仓刻度(total_score≥score_full→满仓 max_weight;按算子集量纲配)
    # 资金分配/风控(可选,YAML risk 段配;None=未配,用 engine 默认)
    max_gross: float | None = None
    stop_loss_pct: float | None = None
    portfolio_brake_dd: float | None = None
    portfolio_brake_scale: float | None = None
    portfolio_brake_mode: str = "scale_all"
    # 组合级敞口刹车:刹车期把总仓上限收窄到该值(<max_gross);keep_ratio=刹车期
    # 只保留 raw 分数最高的比例;add_min_score=回撤加仓仅对 raw≥ 阈值票生效;
    # recover_dd=触发后的解除回撤阈值(滞回,防频繁开关)。
    portfolio_brake_max_gross: float | None = None
    portfolio_brake_keep_ratio: float | None = None
    portfolio_brake_add_min_score: float | None = None
    portfolio_brake_recover_dd: float | None = None
    # 深度分级刹车: ((回撤阈值, 敞口上限), ...) 按回撤深度升序,越深越紧、自然随反弹放松。
    portfolio_brake_tiers: tuple[tuple[float, float], ...] | None = None
    # 滚动新高解除刹车:权益创出 N 日新高即释放(临时熔断自释放;0=不启用)。
    portfolio_brake_recover_high_days: int = 0
    # 分级追踪止盈: ((触发收益率, 从持仓峰值回撤, 卖出比例), ...);卖出比例缺省=1(全清)。
    take_profit_tiers: tuple[tuple[float, ...], ...] | None = None
    take_profit_hard_pct: float | None = None
    # ATR 追踪止盈: period + ((触发收益率, ATR 倍数, 卖出比例), ...)。
    take_profit_atr_period: int | None = None
    take_profit_atr_tiers: tuple[tuple[float, ...], ...] | None = None
    take_profit_atr_lagged: bool = False
    # 大盘趋势 regime 门禁(前瞻性风控,YAML risk.market_regime_* 配;None=未配、用 engine 默认/不启用):
    # trend(ma_days)+ vol(target_vol)双信号,min 叠加到组合敞口上限;详见 engine._market_throttle_step。
    market_regime_code: str | None = None
    market_regime_ma_days: int | None = None
    market_regime_enter_band: float | None = None
    market_regime_exit_band: float | None = None
    market_regime_max_gross: float | None = None
    market_regime_target_vol: float | None = None
    market_regime_vol_window: int | None = None
    market_regime_vol_floor: float | None = None

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
            "score_full": self.score_full,
            "max_gross": self.max_gross,
            "stop_loss_pct": self.stop_loss_pct,
            "portfolio_brake_dd": self.portfolio_brake_dd,
            "portfolio_brake_scale": self.portfolio_brake_scale,
            "portfolio_brake_mode": self.portfolio_brake_mode,
            "portfolio_brake_max_gross": self.portfolio_brake_max_gross,
            "portfolio_brake_keep_ratio": self.portfolio_brake_keep_ratio,
            "portfolio_brake_add_min_score": self.portfolio_brake_add_min_score,
            "portfolio_brake_recover_dd": self.portfolio_brake_recover_dd,
            "portfolio_brake_tiers": self.portfolio_brake_tiers,
            "portfolio_brake_recover_high_days": self.portfolio_brake_recover_high_days,
            "take_profit_tiers": self.take_profit_tiers,
            "take_profit_hard_pct": self.take_profit_hard_pct,
            "take_profit_atr_period": self.take_profit_atr_period,
            "take_profit_atr_tiers": self.take_profit_atr_tiers,
            "take_profit_atr_lagged": self.take_profit_atr_lagged,
            "market_regime_code": self.market_regime_code,
            "market_regime_ma_days": self.market_regime_ma_days,
            "market_regime_enter_band": self.market_regime_enter_band,
            "market_regime_exit_band": self.market_regime_exit_band,
            "market_regime_max_gross": self.market_regime_max_gross,
            "market_regime_target_vol": self.market_regime_target_vol,
            "market_regime_vol_window": self.market_regime_vol_window,
            "market_regime_vol_floor": self.market_regime_vol_floor,
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
    def _ensure_op_meta(self, temperature: float = 0.0):
        """算子元信息预算(每策略算一次,缓存于实例):[(spec, cls, fp, version)]。

        指纹纳入算子源码 hash(sha1(inspect.getsource(cls))[:8]) → 改算子代码自动失效缓存
        (治 P2-5:不再依赖人工 bump version)。回测侧 LLM 已下线,只剩 math 算子;
        Operator.version 降级为人工强制失效开关,日常失效靠 source hash。"""
        cache = getattr(self, "_op_meta_cache", None)
        if cache is None:
            cache = {}
            self._op_meta_cache = cache  # type: ignore[attr-defined]
        if temperature in cache:
            return cache[temperature]
        from stockfu.ai.operator_cache import compute_fingerprint
        meta = []
        for spec in self.operators:
            cls = get_operator_class(spec["id"])
            if cls is None:
                raise ValueError(f"未知算子 '{spec['id']}'(策略 {self.strategy_id})")
            params = dict(spec.get("params") or {})
            version = _load_operator_meta(cls.operator_id)
            source = hashlib.sha1(inspect.getsource(cls).encode("utf-8")).hexdigest()[:8]
            fp = compute_fingerprint(version=version, params=params, source=source)
            meta.append((spec, cls, fp, version))
        cache[temperature] = meta
        return meta

    def begin_run_cache(self, codes: list[str], start, end,
                        temperature: float = 0.0) -> dict:
        """回测启动:区间紧凑预载算子缓存到实例(_run_op_cache)。

        只预载 [start, start+WINDOW] 首块(滚动窗口,见 RUN_CACHE_WINDOW_DAYS),后续
        prefetch_cache 消费到尾部提前量内再同步补块 —— 长区间峰值内存 ≈ 窗口×每日
        行数而非全区间(2007-2026: 11.9M 行 ~3.6G → 250 日窗口 ~180M);范围 ≤ 窗口
        时单块全量 = 旧行为。日期超界/空宇宙安全处理,空则 _run_op_cache={}。
        返回 {days: n_as_of, entries: n_packs}(首块口径,仅日志用)。
        """
        from stockfu.ai.operator_cache import load_operator_results_range

        if start is not None and not isinstance(start, _date):
            start = _date.fromisoformat(str(start)[:10])
        if end is not None and not isinstance(end, _date):
            end = _date.fromisoformat(str(end)[:10])
        meta = self._ensure_op_meta(temperature)
        op_fps = [(spec["id"], fp) for spec, cls, fp, version in meta if fp is not None]
        op_types = {spec["id"]: cls.type for spec, cls, fp, version in meta if fp is not None}
        self._run_op_types = op_types  # type: ignore[attr-defined]
        self._run_op_fps = op_fps  # type: ignore[attr-defined]
        codes = list(codes or [])
        self._run_codes = codes  # type: ignore[attr-defined]
        self._run_end = end  # type: ignore[attr-defined]
        self._run_last_as_of = None  # type: ignore[attr-defined]
        if start is not None and end is not None:
            win_end = min(start + timedelta(days=RUN_CACHE_WINDOW_DAYS), end)
        else:
            win_end = end
        self._run_window_end = win_end  # type: ignore[attr-defined]
        cache = load_operator_results_range(codes, start, win_end, op_fps, op_types=op_types)
        self._run_op_cache = cache  # type: ignore[attr-defined]
        n_entries = sum(len(d) for d in cache.values())
        return {"days": len(cache), "entries": n_entries, "operators": len(op_fps)}

    def _maybe_load_run_cache_window(self, as_of) -> None:
        """滚动预载触发:as_of 进入窗口尾部提前量(或越过)时,同步补下一块。幂等。"""
        wend = getattr(self, "_run_window_end", None)
        end = getattr(self, "_run_end", None)
        if wend is None or end is None or wend >= end:
            return
        d = getattr(as_of, "date", None)
        as_of = d() if d else as_of
        if (wend - as_of).days > RUN_CACHE_LOOKAHEAD_DAYS:
            return
        self._load_next_run_cache_chunk()

    def _load_next_run_cache_chunk(self) -> bool:
        """补下一块 [window_end+1, window_end+WINDOW];按日历日推进(空块也前进,防死循环)。

        与残余尾窗 merge(日期不重叠);已消费日由 prefetch_cache 逐日 pop。无窗口状态
        或已到尾部返回 False。
        """
        from stockfu.ai.operator_cache import load_operator_results_range

        codes = getattr(self, "_run_codes", None) or []
        wend = getattr(self, "_run_window_end", None)
        end = getattr(self, "_run_end", None)
        op_fps = getattr(self, "_run_op_fps", None) or []
        op_types = getattr(self, "_run_op_types", None) or {}
        if not codes or wend is None or end is None or wend >= end:
            return False
        nxt = wend + timedelta(days=1)
        win_end = min(nxt + timedelta(days=RUN_CACHE_WINDOW_DAYS), end)
        chunk = load_operator_results_range(codes, nxt, win_end, op_fps, op_types=op_types)
        if chunk:
            cur = getattr(self, "_run_op_cache", None) or {}
            self._run_op_cache = {**cur, **chunk}  # type: ignore[attr-defined]
        self._run_window_end = win_end  # type: ignore[attr-defined]
        return True

    def end_run_cache(self) -> None:
        """释放区间预载,避免策略实例常驻占内存。"""
        self._run_op_cache = None  # type: ignore[attr-defined]
        self._run_op_types = None  # type: ignore[attr-defined]
        self._run_op_fps = None  # type: ignore[attr-defined]
        self._run_codes = None  # type: ignore[attr-defined]
        self._run_end = None  # type: ignore[attr-defined]
        self._run_window_end = None  # type: ignore[attr-defined]
        self._run_last_as_of = None  # type: ignore[attr-defined]

    def prefetch_cache(self, codes: list[str], as_of,
                       temperature: float = 0.0,
                       max_workers: int = 4) -> dict:
        """单日批量预读 + 冷 miss 并发算 + 批量落库(回测 engine Phase 2 前主线程调一次)。

        有 begin_run_cache 时:从紧凑内存取 hit,miss 再算+写库;窗口消费到尾部提前量内
        自动补下一块(滚动预载,见 RUN_CACHE_WINDOW_DAYS),长区间峰值内存有界。当天
        预填数据在返回给 analyze 后不再需要,必须立刻从区间缓存释放。
        无预载时:回退单日 get_operator_results_batch(兼容旧调用)。
        返回 {(code, op_id): OpResult}。
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from stockfu.ai.operator_cache import (
            get_operator_results_batch,
            prefill_from_run_cache,
            save_operator_results_batch,
        )

        codes = list(codes or [])
        meta = self._ensure_op_meta(temperature)
        op_fps = [(spec["id"], fp) for spec, cls, fp, version in meta if fp is not None]
        op_types = {spec["id"]: cls.type for spec, cls, fp, version in meta if fp is not None}
        if not codes or not op_fps:
            return {}

        run_cache = getattr(self, "_run_op_cache", None)
        if run_cache is not None:
            # 滚动预载触发(可能重分配 _run_op_cache 为合并后新 dict → 重新取引用)
            self._maybe_load_run_cache_window(as_of)
            run_cache = getattr(self, "_run_op_cache", None) or {}
            prefill = prefill_from_run_cache(run_cache, as_of, codes, op_fps, op_types)
            # 回测日历单调递增；该日期之后不会再被访问。prefill 已包含本日值，
            # 因此可释放预载 hit 和随后 miss，保持内存只随单日规模增长。
            run_cache.pop(as_of, None)
            # 运行缓存按日期单调消费；同一日期的重复 prefetch 是幂等查询，
            # 不应在尾部把已经消费完的内存缓存误判成冷 miss 再落库。
            last_as_of = getattr(self, "_run_last_as_of", None)
            if last_as_of is not None and as_of <= last_as_of:
                return prefill
        else:
            prefill = get_operator_results_batch(codes, as_of, op_fps)

        # miss 任务:(code, op_id, cls, params, fp, op_type)
        tasks: list[tuple] = []
        for spec, cls, fp, version in meta:
            if fp is None:
                continue
            op_id = spec["id"]
            params = dict(spec.get("params") or {})
            for c in codes:
                if (c, op_id) not in prefill:
                    tasks.append((c, op_id, cls, params, fp, cls.type))
        if not tasks:
            if run_cache is not None:
                self._run_last_as_of = as_of
            return prefill

        def _eval(task):
            c, op_id, cls, params, fp, op_type = task
            r = cls().run(OpContext(code=c, name="", as_of=as_of), params)
            return c, op_id, fp, op_type, r

        entries: list[tuple] = []
        workers = max(1, int(max_workers or 4))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            fut = {pool.submit(_eval, t): t for t in tasks}
            for f in as_completed(fut):
                try:
                    c, op_id, fp, op_type, r = f.result()
                except Exception:  # noqa: BLE001
                    continue
                prefill[(c, op_id)] = r
                entries.append((c, op_id, fp, op_type, r))
        if entries:
            save_operator_results_batch(as_of, entries)
        if run_cache is not None:
            self._run_last_as_of = as_of
        return prefill

    def analyze(self, code: str, as_of=None, holding_override=None,
                temperature: float = 0.0, cache_prefill: dict | None = None) -> dict:
        # 回测侧 LLM 算子已下线:本方法纯 math,不再调 build_context/narrate。
        # temperature 形参保留(scheduler 传 0.0)仅为签名兼容,已不影响算子输出。
        ctx = OpContext(code=code, name="", as_of=as_of)

        # 1. 跑所有叶子算子(math 先跑,结果进 ctx.factors 供 llm 算子参考)。
        #    算子级缓存:math/llm read-through(operator_result),aggregator 不缓存。
        #    算子元信息(指纹/prompt/version)预算一次(_ensure_op_meta);cache_prefill
        #    命中则跳过 get_operator_result 往返。回测路径 prefetch_cache 已 fill-on-miss
        #    + 批量落库,此处 miss+单行 save 仅兜底(无 prefetch / 预填不全 / 实盘单票)。
        from stockfu.ai.operator_cache import (get_operator_result,
                                               save_operator_result)
        meta = self._ensure_op_meta(temperature)
        results = []
        for spec, cls, fp, version in meta:
            params = dict(spec.get("params") or {})

            # 命中复用:预填 → 单点读;miss → run+save(aggregator fp=None 跳过缓存)
            r = None
            if cache_prefill is not None and fp is not None:
                r = cache_prefill.get((code, spec["id"]))
            if r is None and fp is not None:
                r = get_operator_result(code, as_of, spec["id"], fp)

            if r is None:
                inst = cls()
                r = inst.run(ctx, params)
                if fp is not None:
                    save_operator_result(code, as_of, spec["id"], fp, r, cls.type)

            r.weight = float(spec.get("weight", 1.0))
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
            "target_weight": r.target_weight,
        } for r in results]
        aggregate = {
            "final_signal": summary.signal,
            "total_score": summary.score,
            "risk_vetoed": summary.veto,
            "ai_target_weight": summary.target_weight,
            "confidence": summary.confidence,
        }
        # 买卖权重不对称(opt-in):配置 aggregate.sell_weights 时,额外算卖出总分
        # 并把买入/卖出两个总分各自归一化到 ±100(按各自权重理论上限直接乘)。
        # total_score 保持原始分不动(engine meta["raw"]/横截面排序/门控语义不变);
        # 归一化值仅喂仓位映射(compute_target_weight),死区/满仓刻度即 ±100 刻度。
        sell_weights = self.aggregate.get("sell_weights") or {}
        if sell_weights:
            buy_max = 20.0 * sum(
                float(s.get("weight", 1.0)) for s in self.operators
            )
            sell_max = 20.0 * sum(float(v) for v in sell_weights.values())
            sell_total = round(sum(
                r.score * float(sell_weights.get(r.operator, 1.0))
                for r in results
            ), 2)
            if buy_max > 0:
                aggregate["total_score_norm"] = round(summary.score / buy_max * 100, 2)
            if sell_max > 0:
                aggregate["total_sell_score"] = round(sell_total / sell_max * 100, 2)

        # 4. narrative: 纯数学规则拼接(不调 LLM,秒级)
        parts = "; ".join(f"{r.operator}={r.signal}({r.score:+.1f})" for r in results)
        narrative = f"[{self.name}] {summary.signal} | total={summary.score} | {parts}"

        return {
            "code": code, "name": "",
            "context": {},
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
        tp = rk.get("take_profit") or {}
        tiers = tuple(
            (float(row["profit"]), float(row["drawdown"]),
             float(row.get("sell_fraction", 1.0)))
            for row in tp.get("trailing", [])
            if isinstance(row, dict) and "profit" in row and "drawdown" in row
        )
        atr_cfg = tp.get("atr_trailing") or {}
        atr_tiers = tuple(
            (float(row["profit"]), float(row["multiple"]),
             float(row.get("sell_fraction", 1.0)))
            for row in atr_cfg.get("tiers", [])
            if isinstance(row, dict) and "profit" in row and "multiple" in row
        )
        atr_period = (int(atr_cfg["period"])
                      if atr_cfg.get("period") is not None else None)
        atr_lagged = bool(atr_cfg.get("lagged", False))
        brake_tiers = tuple(
            (float(row["drawdown"]), float(row["max_gross"]))
            for row in (rk.get("portfolio_brake_tiers") or [])
            if isinstance(row, dict) and "drawdown" in row and "max_gross" in row
        )
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
            score_full=p.get("score_full", 20.0),
            max_gross=rk.get("max_gross"),
            stop_loss_pct=rk.get("stop_loss"),
            portfolio_brake_dd=rk.get("portfolio_brake"),
            portfolio_brake_scale=rk.get("portfolio_brake_scale"),
            portfolio_brake_mode=rk.get("portfolio_brake_mode", "scale_all"),
            portfolio_brake_max_gross=rk.get("portfolio_brake_max_gross"),
            portfolio_brake_keep_ratio=rk.get("portfolio_brake_keep_ratio"),
            portfolio_brake_add_min_score=rk.get("portfolio_brake_add_min_score"),
            portfolio_brake_recover_dd=rk.get("portfolio_brake_recover_dd"),
            portfolio_brake_tiers=brake_tiers or None,
            portfolio_brake_recover_high_days=int(rk.get("portfolio_brake_recover_high_days") or 0),
            take_profit_tiers=tiers or None,
            take_profit_hard_pct=(float(tp["hard_profit"])
                                  if tp.get("hard_profit") is not None else None),
            take_profit_atr_period=atr_period,
            take_profit_atr_tiers=atr_tiers or None,
            take_profit_atr_lagged=atr_lagged,
            market_regime_code=rk.get("market_regime_code"),
            market_regime_ma_days=(int(rk["market_regime_ma_days"])
                                   if rk.get("market_regime_ma_days") is not None else None),
            market_regime_enter_band=rk.get("market_regime_enter_band"),
            market_regime_exit_band=rk.get("market_regime_exit_band"),
            market_regime_max_gross=rk.get("market_regime_max_gross"),
            market_regime_target_vol=rk.get("market_regime_target_vol"),
            market_regime_vol_window=(int(rk["market_regime_vol_window"])
                                      if rk.get("market_regime_vol_window") is not None else None),
            market_regime_vol_floor=rk.get("market_regime_vol_floor"),
        )

    @property
    def debounce_kwargs(self) -> dict:
        """旧接口(兼容):返回 to_dict()。新代码用 .debounce_params 拿 dataclass。"""
        return self.debounce_params.to_dict()


def single_operator_fingerprint(operator_id: str, params: dict | None = None) -> str:
    """单算子输入指纹 = hash(version + params + source),复用 _ensure_op_meta 同款算法。

    供 factor_diag 等单算子场景读/写回测算子缓存(operator_result)——指纹与回测逐字一致 →
    跨场景命中复用(回测算过的(code,as_of),因子诊断直接读缓存,反之亦然)。
    source=算子类源码 hash(改算子代码自动失效,治 P2-5,与 _ensure_op_meta 同源)。
    """
    cls = get_operator_class(operator_id)
    if cls is None:
        raise ValueError(f"未知算子 '{operator_id}'(不在注册表)")
    from stockfu.ai.operator_cache import compute_fingerprint
    version = _load_operator_meta(operator_id)
    source = hashlib.sha1(inspect.getsource(cls).encode("utf-8")).hexdigest()[:8]
    return compute_fingerprint(version=version, params=params or {}, source=source)


@functools.lru_cache(maxsize=None)
def _load_operator_meta(operator_id: str) -> int:
    """从 operator 表读算子 version(人工强制失效开关;日常失效靠 source hash)。

    进程级缓存:operator 表低频变更,同进程内每个 operator_id 只查 1 次 DB。
    调用点仅 _ensure_op_meta(已实例级缓存),此 lru_cache 让同进程内第二个
    CompiledStrategy 实例(API 连跑多次回测)也 0 session 开闭。
    """
    from stockfu.db import session_scope
    from stockfu.models import Operator
    with session_scope() as s:
        row = s.get(Operator, operator_id)
        return row.version if row else 1


def compile_strategy(yaml_text: str, *, strategy_id: str = "") -> CompiledStrategy:
    """YAML 文本 → CompiledStrategy。校验算子 id 与汇总方法存在性。

    strategy_id 可选(keyword-only):传入则写入 CompiledStrategy.strategy_id,让变体(base#key
    等复合 id)的 metrics/run_id/产物正确归属;不传则留空,get_active_strategy 仍会权威覆盖。
    """
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
        strategy_id=strategy_id,
        name=cfg.get("name", ""),
        operators=cfg.get("operators", []),
        aggregate=cfg.get("aggregate", {}),
        position=cfg.get("position", {}),
        debounce=cfg.get("debounce", {}),
        risk=cfg.get("risk", {}),
    )


def get_active_strategy() -> CompiledStrategy:
    """读 app_config('active_strategy_id') 指针指向的 strategy 编译;无则兜底 pure_factor。

    active 由单一 app_config key 决定(物理唯一,取代 is_active 多选风险)。
    切策略:set_app_config('active_strategy_id', sid)。
    """
    from stockfu.db import get_app_config, session_scope
    from stockfu.models import Strategy
    if not REGISTRY:
        discover_and_register()
    with session_scope() as s:
        sid = get_app_config("active_strategy_id", "pure_factor")
        row = s.get(Strategy, sid)
        if row is None:
            row = s.get(Strategy, "pure_factor")
        if row is None:
            raise RuntimeError("无可用策略(operator/strategy 表未 seed?)")
        cs = compile_strategy(row.config)
        cs.strategy_id = row.strategy_id
        cs.name = cs.name or row.name
        return cs
