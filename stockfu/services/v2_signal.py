"""V2 单日策略评分(信号邮件用):复用回测引擎的评分原语,跑只含评分+历史的最小循环。

设计(A2):不跑回测引擎的交易/账户/风控,只复用 ``HistoryState`` / ``FactorScorer`` /
``AlphaAggregator`` / raw_computers / 行情预载。对目标 as_of 评分前,先回放预热期把
HistoryState 喂满(评分读 cutoff<as_of 的历史,防未来函数)。

性能:非采样日只推进 ``history.cutoff`` 不算 raw(``history.update(t,{},...)`` 仍设 cutoff),
全量 raw 计算只发生在月末采样日 + as_of。5 年预热对 800 股 × 10 metric 约 30-60s。

粒度(方案①):直接用 ``strategy_score``(已 guaranteed [0,100]、50 中性,见
``contracts.StrategyScoreObservation`` 注释「禁止再映射」),不做任何再映射;跨策略分布
差异通过校准元数据(P05/中位数/P95/饱和率)在邮件层暴露,而非隐藏。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from stockfu.backtest.engine import (
    _backtest_series_ctx,
    _get_day_market,
    _preload_dividend_events,
    _preload_financial_reports,
    _preload_market_range,
    _trade_calendar_days,
)
from stockfu.backtest.v2_engine import (
    _PRELOAD_LOOKBACK_DAYS,
    _load_listing_and_industry,
    _validate_raw_observation,
)
from stockfu.backtest.v2_run import (
    RAW_COMPUTERS,
    _load,
    historical_full_universe,
    historical_full_universe_rules,
)
from stockfu.factors.raw import raw_fingerprint
from stockfu.scoring.contracts import ScoreStatus, StrategyScoreObservation
from stockfu.scoring.history import (
    HistoryState,
    build_history_retention,
    compute_sample_dates,
)
from stockfu.scoring.profiles import FactorProfile, profile_from_dict
from stockfu.scoring.scorer import FactorScorer
from stockfu.services.universe import DayFlags, UniverseContext
from stockfu.strategy.alpha import AlphaAggregator, AlphaDefinition, alpha_from_dict

# 本轮「十策略」研究 alpha(不含原有的 dividend_low_vol_v2)。顺序即邮件列顺序。
TEN_RESEARCH_ALPHAS: list[str] = [
    "multi_factor_v2",
    "value_ep_bp_v2",
    "dividend_income_v2",
    "low_volatility_pure_v2",
    "defensive_low_beta_v2",
    "momentum_jt_v2",
    "fifty_two_week_high_v2",
    "trend_following_v2",
    "reversal_jl_v2",
    "rsi_reversal_v2",
]

# history_specs 名 → HistoryState.update 的 sample_flags 短名(与 v2_engine._COMP_SHORT 一致)
_COMP_SHORT = {
    "self_history": "self",
    "market_history": "market",
    "industry_history": "industry",
}


# 正式五套荐股策略的中文名（荐股报告/邮件/控制台统一展示）。
# 其余研究 alpha 仍走英文可读名回退（_alpha_display_name 内处理）。
ALPHA_CN_NAMES: dict[str, str] = {
    "value_ep_bp_equal_v2": "价值",
    "dividend_income_history45_v2": "高股息",
    "multi_factor_value_tilt_v2": "多因子",
    "multi_factor_quality_v2": "质量增强",
    "earnings_momentum_offense_v2": "盈利动量进攻",
}


def _alpha_display_name(alpha: AlphaDefinition) -> str:
    """alpha_id → 可读名；正式五套用中文名，其余去 _v2 后缀 + 空格。"""
    aid = alpha.alpha_id
    if aid in ALPHA_CN_NAMES:
        return ALPHA_CN_NAMES[aid]
    for suffix in ("_v2", "_v1"):
        if aid.endswith(suffix):
            aid = aid[: -len(suffix)]
            break
    return aid.replace("_", " ").strip()


@dataclass
class V2SignalReport:
    """单日 V2 评分报告(邮件数据源)。"""

    as_of: date
    alpha_ids: list[str]
    alpha_names: dict[str, str]
    universe_size: int
    n_scored: int
    # 每只股票:{code, name, scores:{alpha_id:{score, status, coverage}}}
    rows: list[dict[str, Any]]
    # 每策略横截面校准(粒度①:暴露分布差异,供邮件图例展示)
    calibration: dict[str, dict[str, float]]


def _percentile(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    rank = (p / 100.0) * (n - 1)
    lo = int(rank)
    hi = min(lo + 1, n - 1)
    frac = rank - lo
    return round(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac, 2)


class V2SignalScorer:
    """对一组 V2 alpha 在单个 as_of 上做横截面评分。

    复用回测引擎的评分原语,但不跑交易/账户/风控。每次 ``score`` 自带预热:
    从 ``history_origin`` 回放到 as_of,把 HistoryState 喂满后再评 as_of。
    """

    def __init__(
        self,
        alpha_ids: list[str] | None = None,
        *,
        universe_rules=None,
        codes: list[str] | None = None,
        history_years: int = 5,
        benchmark_code: str = "sh000300",
    ) -> None:
        self.alpha_ids = list(alpha_ids or TEN_RESEARCH_ALPHAS)
        self.benchmark_code = benchmark_code
        self.history_years = history_years

        # ---- 装配:加载 alpha + 取 profile/raw_computer 并集 ----
        self.alphas: dict[str, AlphaDefinition] = {}
        self.aggregators: dict[str, AlphaAggregator] = {}
        self.profiles: dict[str, FactorProfile] = {}
        self.raw_computers: dict[str, Any] = {}
        self.raw_params: dict[str, dict] = {}
        self.raw_fingerprints: dict[str, str] = {}
        self.metric_units: dict[str, str] = {}
        self.alpha_names: dict[str, str] = {}

        for aid in self.alpha_ids:
            alpha = alpha_from_dict(_load(f"alphas/{aid}.yaml"))
            self.alphas[aid] = alpha
            self.aggregators[aid] = AlphaAggregator(alpha)
            self.alpha_names[aid] = _alpha_display_name(alpha)
            for f in alpha.factors:
                if f.profile_id not in self.profiles:
                    p = profile_from_dict(_load(f"factor_profiles/{f.profile_id}.yaml"))
                    self.profiles[f.profile_id] = p
                    self._register_raw(p)

        self.alpha_profile_ids: dict[str, list[str]] = {
            aid: [f.profile_id for f in self.alphas[aid].factors] for aid in self.alpha_ids
        }
        self.pid_to_metric = {
            pid: p.raw_metric_id for pid, p in self.profiles.items()
        }
        self.market_scope = next(
            (a.market_scope for a in self.alphas.values()), "cn_equity"
        )

        # ---- 宇宙 ----
        if universe_rules is None:
            universe_rules = historical_full_universe_rules()
        self.universe_rules = universe_rules
        self.codes = list(codes) if codes is not None else historical_full_universe()

    def _register_raw(self, p: FactorProfile) -> None:
        metric = p.raw_metric_id
        if metric not in RAW_COMPUTERS:
            raise KeyError(f"raw_metric_id {metric} 未在 RAW_COMPUTERS 登记")
        if metric not in self.raw_computers:
            spec = RAW_COMPUTERS[metric]
            self.raw_computers[metric] = spec.fn
            self.raw_params[metric] = dict(p.raw_metric_params)
            self.raw_fingerprints[metric] = raw_fingerprint(
                metric, spec.algo, self.raw_params[metric]
            )
        # 单位一致性(同一 metric 只允许一个 raw_unit)
        old = self.metric_units.get(metric)
        if old is not None and old != p.raw_unit:
            raise ValueError(
                f"raw_metric_id={metric} 被不同 raw_unit 引用: {old!r} vs {p.raw_unit!r}"
            )
        self.metric_units[metric] = p.raw_unit

    # ----------------------------------------------------------------- 评分

    def score(self, as_of: date) -> V2SignalReport:
        history_origin = date(
            as_of.year - self.history_years, as_of.month, as_of.day
        )
        pre_start = history_origin - timedelta(days=_PRELOAD_LOOKBACK_DAYS)
        preload_codes = sorted({*self.codes, self.benchmark_code})

        # 行情列式预载(覆盖 raw 最大回看 + self 历史)
        sctx = _preload_market_range(preload_codes, pre_start, as_of)
        div_index = _preload_dividend_events(self.codes, pre_start, as_of)
        # 财务三表一次预载 → 质量因子(quality_roe/gross_margin/leverage 等)零逐票查库
        fin_index = _preload_financial_reports(self.codes, as_of)
        listing, industry = _load_listing_and_industry(self.codes)
        uni_ctx = UniverseContext.load(self.codes, self.universe_rules)

        # as_of 可能超过库数据末日(交易日历预埋了未来日);截断到实际数据末日,
        # 与回测引擎的 data_end 截断一致——否则目标日无 bar → 0 评分。
        data_end = max(sctx.dates) if (sctx and sctx.dates) else as_of
        if data_end < as_of:
            as_of = data_end

        # 采样日集合(确定性,只由 sampling 规则决定)
        sampling_calendar = _trade_calendar_days(history_origin, as_of + timedelta(days=31))
        sample_dates: dict[tuple[str, str, str], set[date]] = {}
        for p in self.profiles.values():
            for comp, spec in p.history_specs.items():
                key = (p.raw_metric_id, comp, spec.sampling)
                if key not in sample_dates:
                    sample_dates[key] = {
                        d for d in compute_sample_dates(sampling_calendar, spec.sampling)
                        if d <= as_of
                    }
        all_sample_dates: set[date] = set()
        for s in sample_dates.values():
            all_sample_dates |= s

        history = HistoryState(
            retention=build_history_retention(self.profiles.values()))
        scorers = {pid: FactorScorer(self.profiles[pid]) for pid in self.profiles}
        trade_days = _trade_calendar_days(history_origin, as_of)

        # ---- 回放预热 + 评分 as_of(挂内存供给器 → raw 算子零查库)----
        strategy_obs: dict[str, dict[str, StrategyScoreObservation]] = {}
        with _backtest_series_ctx(sctx, div_index, financial_index=fin_index):
            for t in trade_days:
                is_sample = t in all_sample_dates
                is_target = t == as_of
                if not (is_sample or is_target):
                    # 非采样日:只推进 cutoff(保持与回测一致的窗口边界)
                    history.update(t, {}, industry, self.market_scope, {})
                    continue

                close_q, _open_q, day_bars = _get_day_market(self.codes, t, sctx)
                day_flags: dict[str, DayFlags] = {}
                for c in self.codes:
                    bar = day_bars.get(c)
                    if bar:
                        day_flags[c] = DayFlags(
                            is_st=bool(bar.get("is_st")),
                            trade_status=int(bar.get("trade_status", 1)),
                            has_row=True,
                            amount=bar.get("amount"),
                        )
                    else:
                        day_flags[c] = DayFlags(has_row=False)
                eligible = {c for c in uni_ctx.eligible_on(t, day_flags) if c in close_q}
                if not eligible:
                    history.update(t, {}, industry, self.market_scope, {})
                    continue

                # 当日原始值(逐 metric × eligible code)
                raw_by_metric: dict[str, dict[str, Any]] = {}
                for metric, computer in self.raw_computers.items():
                    m: dict[str, Any] = {}
                    params = self.raw_params.get(metric, {})
                    for c in sorted(eligible):
                        obs = computer(c, t, **params) if params else computer(c, t)
                        _validate_raw_observation(
                            obs, metric, t, self.metric_units[metric], c,
                            self.raw_fingerprints.get(metric, ""),
                        )
                        m[c] = obs
                    raw_by_metric[metric] = m

                # 评分(仅 as_of 当日;读 cutoff<as_of 的历史)
                if is_target:
                    cutoff = history.cutoff
                    for sc in scorers.values():
                        sc.new_day()
                    for aid in self.alpha_ids:
                        pids = self.alpha_profile_ids[aid]
                        agg = self.aggregators[aid]
                        per_code: dict[str, StrategyScoreObservation] = {}
                        for c in sorted(eligible):
                            fs = {}
                            for pid in pids:
                                metric = self.pid_to_metric[pid]
                                fs[pid] = scorers[pid].score(
                                    raw_by_metric[metric][c], history,
                                    industry.get(c), self.market_scope, cutoff,
                                )
                            per_code[c] = agg.aggregate(
                                c, t, fs, reference_cutoff=cutoff,
                                universe_status="in_universe", observation=False,
                            )
                        strategy_obs[aid] = per_code

                # 日末:追加 t 日观测到历史(评分后)
                self._update_history(history, t, raw_by_metric, industry, sample_dates)

        return self._build_report(as_of, strategy_obs)

    def _update_history(
        self, history: HistoryState, t: date,
        raw_by_metric: dict[str, dict[str, Any]], industry: dict[str, str | None],
        sample_dates: dict[tuple[str, str, str], set[date]],
    ) -> None:
        metric_values: dict[str, dict[str, float | None]] = {}
        sample_flags: dict[str, dict[str, bool]] = {}
        for p in self.profiles.values():
            m = p.raw_metric_id
            if m not in metric_values:
                metric_values[m] = {
                    c: obs.raw_value for c, obs in raw_by_metric.get(m, {}).items()
                }
                flags: dict[str, bool] = {}
                for comp, spec in p.history_specs.items():
                    flags[_COMP_SHORT[comp]] = t in sample_dates[(m, comp, spec.sampling)]
                sample_flags[m] = flags
        history.update(t, metric_values, industry, self.market_scope, sample_flags)

    # ------------------------------------------------------------- 报告组装

    def _build_report(
        self, as_of: date,
        strategy_obs: dict[str, dict[str, StrategyScoreObservation]],
    ) -> V2SignalReport:
        # 收集每只股票对每个 alpha 的分;同时统计每 alpha 横截面校准
        codes_seen: set[str] = set()
        for per_code in strategy_obs.values():
            codes_seen |= set(per_code.keys())

        # 股票名
        name_map: dict[str, str] = {}
        if codes_seen:
            from sqlmodel import select

            from stockfu.db import session_scope
            from stockfu.models import Asset, SecurityMaster

            with session_scope() as s:
                for row in s.exec(
                    select(SecurityMaster).where(SecurityMaster.code.in_(codes_seen))
                ).all():
                    name_map[row.code] = row.name or ""
                for row in s.exec(
                    select(Asset).where(Asset.code.in_(codes_seen))
                ).all():
                    if row.name:
                        name_map.setdefault(row.code, row.name)

        rows: list[dict[str, Any]] = []
        # 每 alpha 的已评分(且 tradable)分列表,用于校准
        cal_arrays: dict[str, list[float]] = {aid: [] for aid in self.alpha_ids}
        cal_tradable: dict[str, int] = {aid: 0 for aid in self.alpha_ids}
        cal_total: dict[str, int] = {aid: 0 for aid in self.alpha_ids}

        for c in codes_seen:
            scores: dict[str, dict[str, Any]] = {}
            mean_vals: list[float] = []
            for aid in self.alpha_ids:
                obs = strategy_obs.get(aid, {}).get(c)
                if obs is None:
                    continue
                scores[aid] = {
                    "score": round(float(obs.strategy_score), 2),
                    "status": obs.score_status.value,
                    "coverage": round(float(obs.effective_coverage), 3),
                }
                cal_total[aid] += 1
                if obs.score_status == ScoreStatus.TRADABLE:
                    cal_tradable[aid] += 1
                    cal_arrays[aid].append(float(obs.strategy_score))
                    mean_vals.append(float(obs.strategy_score))
            if not scores:
                continue
            rows.append({
                "code": c,
                "name": name_map.get(c, ""),
                "scores": scores,
                "_mean": sum(mean_vals) / len(mean_vals) if mean_vals else 0.0,
            })

        # 排序:可交易均分降序,再按 code
        rows.sort(key=lambda r: (-r["_mean"], r["code"]))
        for r in rows:
            r.pop("_mean", None)

        calibration: dict[str, dict[str, float]] = {}
        for aid in self.alpha_ids:
            vals = sorted(cal_arrays[aid])
            n = len(vals)
            sat = sum(1 for v in vals if v <= 0.0 or v >= 100.0)
            calibration[aid] = {
                "n": n,
                "p05": _percentile(vals, 5),
                "p50": _percentile(vals, 50),
                "p95": _percentile(vals, 95),
                "saturation_0_100": round(sat / n, 3) if n else None,
                "tradable_pct": round(cal_tradable[aid] / cal_total[aid], 3)
                if cal_total[aid]
                else None,
            }

        return V2SignalReport(
            as_of=as_of,
            alpha_ids=list(self.alpha_ids),
            alpha_names=dict(self.alpha_names),
            universe_size=len(codes_seen),
            n_scored=len(rows),
            rows=rows,
            calibration=calibration,
        )
