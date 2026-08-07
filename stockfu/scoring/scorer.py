"""因子评分器:把一个 RawFactorObservation 经 profile 映射成 FactorScoreObservation。

职责(设计 §4 第5层):原始值 → 统一方向的 0–100。不组合因子、不决定仓位。

性能要点:同一评分日 t、同一 market_scope/industry 的历史分位对所有股票共用同一
参考分布(§9.3 step4「使用步骤2的同一状态为全部股票评分」)。因此 market/industry
的样本序列当日只取一次,跨 code 复用;self 历史是 per-code 的,逐 code 取。
"""
from __future__ import annotations

import math

from stockfu.scoring.contracts import FactorScoreObservation, RawFactorObservation
from stockfu.scoring.history import HistoryState
from stockfu.scoring.mappings import combine_hybrid, ecdf_score, ecdf_score_sorted
from stockfu.scoring.profiles import FactorProfile


class FactorScorer:
    """单个 profile 的评分器。"""

    def __init__(self, profile: FactorProfile) -> None:
        self.profile = profile
        self._market_sorted: dict[tuple, list[float]] = {}
        self._industry_sorted: dict[tuple, list[float]] = {}
        # 缓存:profile 不可变 → mapping_fingerprint 整回测算一次;state_hash 只依赖
        # (metric, cutoff),同一天所有票相同 → 每天(每个 cutoff)算一次跨票复用。
        # 不缓存则每票每次评分都全量遍历 history + JSON 序列化,随 history 增长越来越慢。
        self._mapping_fp: str | None = None
        self._state_hash: str | None = None

    def new_day(self) -> None:
        """每个评分日开始前清缓存:market/industry 样本随 cutoff 变。"""
        self._market_sorted.clear()
        self._industry_sorted.clear()
        self._state_hash = None      # cutoff 推进 → 重算(_mapping_fp 不清:profile 不变)

    def score(self, raw: RawFactorObservation, history: HistoryState,
              industry: str | None, market_scope: str, cutoff) -> FactorScoreObservation:
        p = self.profile
        specs = p.history_specs
        cutoff_key = cutoff.isoformat() if cutoff is not None else None
        components = {}
        if self._mapping_fp is None:
            self._mapping_fp = p.mapping_fingerprint()
        if self._state_hash is None:
            self._state_hash = history.state_hash(p.raw_metric_id, market_scope)

        if "self_history" in specs:
            sp = specs["self_history"]
            samples = history.self_samples(
                p.raw_metric_id, raw.asset_code, cutoff, sp.years, sp.state)
            components["self_history"] = ecdf_score(
                raw.raw_value, samples, p.direction, sp.min_observations)

        if "market_history" in specs:
            sp = specs["market_history"]
            key = (p.raw_metric_id, market_scope, cutoff_key, sp.years, sp.state)
            sorted_s = self._market_sorted.get(key)
            if sorted_s is None:
                samples = history.market_samples(
                    p.raw_metric_id, market_scope, cutoff, sp.years, sp.state)
                sorted_s = sorted(x for x in samples
                                  if x is not None and not (isinstance(x, float) and math.isnan(x)))
                self._market_sorted[key] = sorted_s
            components["market_history"] = ecdf_score_sorted(
                raw.raw_value, sorted_s, p.direction, sp.min_observations)

        if "industry_history" in specs:
            sp = specs["industry_history"]
            key = (p.raw_metric_id, industry, cutoff_key, sp.years, sp.state)
            sorted_s = self._industry_sorted.get(key)
            if sorted_s is None:
                samples = history.industry_samples(
                    p.raw_metric_id, industry, cutoff, sp.years, sp.state)
                sorted_s = sorted(x for x in samples
                                  if x is not None and not (isinstance(x, float) and math.isnan(x)))
                self._industry_sorted[key] = sorted_s
            components["industry_history"] = ecdf_score_sorted(
                raw.raw_value, sorted_s, p.direction, sp.min_observations)

        abs_knots = p.absolute_knots if p.absolute_weight > 0 else None
        result = combine_hybrid(
            raw.raw_value, direction=p.direction, absolute_knots=abs_knots,
            weights=p.weights(), components=components, missing_reason=raw.missing_reason)

        return FactorScoreObservation(
            profile_id=p.profile_id, profile_version=p.version,
            asset_code=raw.asset_code, as_of=raw.as_of,
            raw_metric_id=p.raw_metric_id, score=result.score,
            evidence_coverage=result.evidence_coverage, maturity=result.maturity,
            mapping_fingerprint=self._mapping_fp, reference_cutoff=cutoff,
            absolute_score=result.absolute_score,
            market_history_score=result.market_history_score,
            industry_history_score=result.industry_history_score,
            self_history_score=result.self_history_score,
            raw_value=raw.raw_value, history_n=result.history_n,
            state_hash=self._state_hash,
            warnings=list(result.warnings), missing_reason=raw.missing_reason,
            raw_fingerprint=raw.raw_fingerprint,
            formal_requires_mature=p.formal_requires_mature,
        )
