"""历史参考状态(设计 §8、§9.3、§14)。

三类状态(均按 raw_metric_id 维护):
- self:per-code 精确日值(rolling by years)。
- market / industry:按采样日分组的截面值池(可精确逐出旧采样日)。

不变量(防未来函数):
- t 日评分只读 cutoff < t 的状态;update(t) 只追加 t 日观测,且必须在
  「所有 t 日评分完成后」调用(§9.3 step4 vs step8)。
- 插入顺序不影响结果:self/market/industry 的 ECDF 全部基于排序 + 计数(§8.3)。
- 采样确定性:只由 (date, code, profile sampling) 决定,无随机 reservoir。
"""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Iterable

from stockfu.scoring.contracts import fingerprint
from stockfu.scoring.profiles import (
    SAMPLE_DAILY,
    SAMPLE_MONTH_END,
    SAMPLE_MONTH_END_CROSS,
    SAMPLE_WEEKEND_CROSS,
)

_DAYS_PER_YEAR = 365.25


def _window_lo(cutoff: date, years: float) -> date:
    return cutoff - timedelta(days=int(years * _DAYS_PER_YEAR))


def compute_sample_dates(dates: Iterable[date], sampling: str) -> set[date]:
    """交易日序列 → 满足 sampling 的采样日集合(确定性)。

    - daily:全部交易日。
    - month_end / month_end_cross_section:当月最后一个交易日(有后继交易日且下一日跨月)。
    - weekend_cross_section:当周最后一个交易日(有后继交易日且下一日跨周或长假)。

    输入序列必须包含用于判断末端边界的少量后继交易日；故意不把输入末日
    自动视为采样日，避免短回测/延长回测产生不同历史状态。
    """
    ds = sorted(dates)
    if sampling == SAMPLE_DAILY:
        return set(ds)
    out: set[date] = set()
    for i, d in enumerate(ds):
        nxt = ds[i + 1] if i + 1 < len(ds) else None
        if sampling in (SAMPLE_MONTH_END, SAMPLE_MONTH_END_CROSS):
            if nxt is not None and nxt.month != d.month:
                out.add(d)
        elif sampling == SAMPLE_WEEKEND_CROSS:
            if nxt is not None and (
                (nxt - d).days > 4 or nxt.isocalendar()[:2] != d.isocalendar()[:2]
            ):
                out.add(d)
    return out


class HistoryState:
    """历史参考状态机。所有写入按 as_of 升序追加(回测逐日推进)。"""

    def __init__(self) -> None:
        # metric -> code -> [(date, value)]  (按 date 升序)
        self._self: dict[str, dict[str, list[tuple[date, float]]]] = defaultdict(
            lambda: defaultdict(list))
        # metric -> scope -> [(date, [values])]  (按 date 升序)
        self._market: dict[str, dict[str, list[tuple[date, list[float]]]]] = defaultdict(
            lambda: defaultdict(list))
        # metric -> industry -> [(date, [values])]
        self._industry: dict[str, dict[str, list[tuple[date, list[float]]]]] = defaultdict(
            lambda: defaultdict(list))
        self.cutoff: date | None = None

    # ----------------------------------------------------------- 写入(§9.3 step8)

    def update(self, as_of: date,
               metric_values: dict[str, dict[str, float | None]],
               industry_of: dict[str, str | None],
               market_scope: str,
               sample_flags: dict[str, dict[str, bool]]) -> None:
        """将 t 日合格观测追加到状态。必须在全部 t 日评分完成后调用。

        metric_values: {raw_metric_id: {code: raw_value_or_None}}。
        sample_flags: {raw_metric_id: {'self':bool,'market':bool,'industry':bool}}。
        """
        for metric in sorted(metric_values):
            codevals = metric_values[metric]
            flags = sample_flags.get(metric, {})
            # 固定 code 顺序，既保证 checkpoint 稳定，也避免同分资产的结果
            # 受到 set/dict 插入顺序影响。
            clean = {c: codevals[c] for c in sorted(codevals) if codevals[c] is not None}

            if flags.get("self", False):
                st = self._self[metric]
                for code, val in clean.items():
                    st[code].append((as_of, float(val)))

            if flags.get("market", False):
                vals = sorted(clean.values())
                if vals:
                    self._market[metric][market_scope].append((as_of, vals))

            if flags.get("industry", False):
                by_ind: dict[str, list[float]] = defaultdict(list)
                for code, val in clean.items():
                    ind = industry_of.get(code)
                    if ind:
                        by_ind[ind].append(float(val))
                for ind in sorted(by_ind):
                    self._industry[metric][ind].append((as_of, by_ind[ind]))
        self.cutoff = as_of

    # ----------------------------------------------------------- 读取(§9.3 step2-4)

    def self_samples(self, metric: str, code: str, cutoff: date | None,
                     years: float, state: str = "rolling") -> list[float]:
        arr = self._self.get(metric, {}).get(code)
        if not arr or cutoff is None:
            return []
        dates = [d for d, _v in arr]
        l = 0 if state == "expanding" else bisect_right(dates, _window_lo(cutoff, years))
        r = bisect_right(dates, cutoff)
        return [arr[i][1] for i in range(l, r)]

    def _pool_samples(self, pool, metric, key, cutoff, years, state="rolling"):
        arr = pool.get(metric, {}).get(key)
        if not arr or cutoff is None:
            return []
        dates = [d for d, _v in arr]
        l = 0 if state == "expanding" else bisect_right(dates, _window_lo(cutoff, years))
        r = bisect_right(dates, cutoff)
        out: list[float] = []
        for i in range(l, r):
            out.extend(arr[i][1])
        return out

    def market_samples(self, metric: str, scope: str, cutoff: date | None,
                       years: float, state: str = "rolling") -> list[float]:
        return self._pool_samples(self._market, metric, scope, cutoff, years, state)

    def industry_samples(self, metric: str, industry: str | None, cutoff: date | None,
                         years: float, state: str = "rolling") -> list[float]:
        if not industry:
            return []
        return self._pool_samples(self._industry, metric, industry, cutoff, years, state)

    # ----------------------------------------------------------- checkpoint(§14.2)

    def to_checkpoint(self) -> dict[str, Any]:
        """确定性序列化:可无损恢复,恢复后下一日输出与连续运行逐位一致。"""
        return {
            "self": {
                m: {c: [[d.isoformat(), v] for d, v in codes[c]]
                    for c in sorted(codes)}
                for m, codes in sorted(self._self.items())
            },
            "market": {
                m: {sc: [[d.isoformat(), vs] for d, vs in scopes[sc]]
                    for sc in sorted(scopes)}
                for m, scopes in sorted(self._market.items())
            },
            "industry": {
                m: {ind: [[d.isoformat(), vs] for d, vs in inds[ind]]
                    for ind in sorted(inds)}
                for m, inds in sorted(self._industry.items())
            },
            "cutoff": self.cutoff.isoformat() if self.cutoff else None,
        }

    @classmethod
    def from_checkpoint(cls, data: dict[str, Any]) -> "HistoryState":
        h = cls()
        for m, codes in data.get("self", {}).items():
            for c, arr in codes.items():
                h._self[m][c] = [(date.fromisoformat(d), float(v)) for d, v in arr]
        for m, scopes in data.get("market", {}).items():
            for sc, arr in scopes.items():
                h._market[m][sc] = [(date.fromisoformat(d), [float(x) for x in vs]) for d, vs in arr]
        for m, inds in data.get("industry", {}).items():
            for ind, arr in inds.items():
                h._industry[m][ind] = [(date.fromisoformat(d), [float(x) for x in vs]) for d, vs in arr]
        co = data.get("cutoff")
        h.cutoff = date.fromisoformat(co) if co else None
        return h

    def state_hash(self, metric: str, scope: str | None = None) -> str:
        """某 raw_metric 状态摘要(供 FactorScoreObservation.state_hash)。"""
        snap = {
            "metric": metric,
            "scope": scope,
            "self_n": {c: len(arr) for c, arr in self._self.get(metric, {}).items()},
            "market_n": {sc: [len(arr), sum(len(v) for _, v in arr)]
                         for sc, arr in self._market.get(metric, {}).items()},
            "industry_n": {ind: [len(arr), sum(len(v) for _, v in arr)]
                           for ind, arr in self._industry.get(metric, {}).items()},
            "cutoff": self.cutoff.isoformat() if self.cutoff else None,
        }
        return fingerprint(snap, prefix="history.state")
