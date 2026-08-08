"""V2 映射算法单测(对应设计 §22.1)。"""
from __future__ import annotations

import math

import pytest

from stockfu.scoring.contracts import Direction, Maturity
from stockfu.scoring.mappings import (
    HistoryComponent,
    clamp,
    combine_hybrid,
    ecdf_score,
    fixed_score,
)

BETA = [(0.0, 100.0), (1.0, 50.0), (2.0, 0.0)]   # 低 Beta 更好


# -------------------------------------------------------------------- fixed


def test_fixed_endpoints_and_clamp():
    # 越界截断到端点分数(不是 0/100)
    assert fixed_score(-1.0, BETA) == 100.0
    assert fixed_score(0.0, BETA) == 100.0
    assert fixed_score(2.0, BETA) == 0.0
    assert fixed_score(3.0, BETA) == 0.0
    # 锚点精确命中
    assert fixed_score(1.0, BETA) == 50.0
    # 区间内线性
    assert fixed_score(0.8, BETA) == pytest.approx(60.0)
    assert fixed_score(1.2, BETA) == pytest.approx(40.0)


def test_fixed_linear_midpoint_strict():
    # 相邻锚点间严格等比例:中点 score = 两端均值
    knots = [(0.0, 0.0), (2.0, 100.0)]
    assert fixed_score(1.0, knots) == pytest.approx(50.0)
    assert fixed_score(0.5, knots) == pytest.approx(25.0)
    assert fixed_score(1.5, knots) == pytest.approx(75.0)


def test_fixed_monotonic_and_nan():
    knots = [(0.0, 0.0), (5.0, 50.0), (12.0, 100.0)]
    prev = -1.0
    for x in [0, 1, 3, 5, 8, 12]:
        s = fixed_score(float(x), knots)
        assert s is not None and s >= prev
        prev = s
    assert fixed_score(None, knots) is None
    assert fixed_score(float("nan"), knots) is None


# --------------------------------------------------------------------- ECDF


def test_ecdf_midrank():
    samples = [1.0, 2.0, 2.0, 3.0]
    # raw=2: L=1, E=2, N=4 → pct=(1+0.5*2)/4=0.5
    h = ecdf_score(2.0, samples, Direction.HIGHER_IS_BETTER, 10)
    assert h.score == pytest.approx(50.0)
    assert h.n == 4
    # raw=1: L=0,E=1 → 0.125
    assert ecdf_score(1.0, samples, Direction.HIGHER_IS_BETTER, 10).score == pytest.approx(12.5)
    # raw=3: L=3,E=1 → 0.875
    assert ecdf_score(3.0, samples, Direction.HIGHER_IS_BETTER, 10).score == pytest.approx(87.5)
    # lower_is_better 翻转
    assert ecdf_score(2.0, samples, Direction.LOWER_IS_BETTER, 10).score == pytest.approx(50.0)
    assert ecdf_score(1.0, samples, Direction.LOWER_IS_BETTER, 10).score == pytest.approx(87.5)


def test_ecdf_order_invariant():
    a = [5.0, 1.0, 3.0, 3.0, 2.0]
    b = [3.0, 1.0, 5.0, 2.0, 3.0]
    for x in [1.0, 2.0, 3.0, 4.0, 5.0]:
        assert ecdf_score(x, a, Direction.HIGHER_IS_BETTER, 10).score == \
            ecdf_score(x, b, Direction.HIGHER_IS_BETTER, 10).score


def test_ecdf_empty_and_maturity():
    assert ecdf_score(1.0, [], Direction.HIGHER_IS_BETTER, 10).score is None
    h = ecdf_score(1.0, [1.0] * 50, Direction.HIGHER_IS_BETTER, 100)
    assert h.maturity_coef == pytest.approx(0.5)
    h2 = ecdf_score(1.0, [1.0] * 200, Direction.HIGHER_IS_BETTER, 100)
    assert h2.maturity_coef == 1.0


# ------------------------------------------------------------------- hybrid


def _comp(score: float, n: int, m: float = 1.0) -> HistoryComponent:
    return HistoryComponent(score=score, n=n, maturity_coef=m)


def test_hybrid_full_mature():
    # abs 0.5 (score 80) + market 0.5 (score 60), 全成熟
    # 50 + 0.5*1*(80-50) + 0.5*1*(60-50) = 50+15+5 = 70
    r = combine_hybrid(
        80.0, direction=Direction.HIGHER_IS_BETTER,
        absolute_knots=[(0.0, 0.0), (100.0, 100.0)],
        weights={"absolute": 0.5, "market_history": 0.5},
        components={"market_history": _comp(60.0, 1000)},
    )
    assert r.score == pytest.approx(70.0)
    assert r.evidence_coverage == pytest.approx(1.0)
    assert r.maturity == Maturity.MATURE


def test_hybrid_missing_raw_is_50_immature():
    r = combine_hybrid(
        None, direction=Direction.HIGHER_IS_BETTER,
        absolute_knots=[(0.0, 0.0), (100.0, 100.0)],
        weights={"absolute": 0.5, "market_history": 0.5},
        components={"market_history": _comp(60.0, 1000)},
    )
    assert r.score == 50.0
    assert r.evidence_coverage == 0.0
    assert r.maturity == Maturity.IMMATURE


def test_hybrid_partial_history_shrinks_to_50():
    # market 未成熟 m=0.5,score=100(远离50);abs score=100,m=1
    # 未成熟分量被收缩:50 + 0.5*1*(100-50) + 0.5*0.5*(100-50) = 50+25+12.5=87.5
    r = combine_hybrid(
        100.0, direction=Direction.HIGHER_IS_BETTER,
        absolute_knots=[(0.0, 0.0), (100.0, 100.0)],
        weights={"absolute": 0.5, "market_history": 0.5},
        components={"market_history": _comp(100.0, 50, 0.5)},
    )
    assert r.score == pytest.approx(87.5)
    assert r.evidence_coverage == pytest.approx(0.75)
    assert r.maturity == Maturity.PARTIAL


def test_hybrid_missing_component_does_not_inflate():
    # market 无样本 → 不参与,coverage<1,且不会因重归一化制造极端分
    r = combine_hybrid(
        60.0, direction=Direction.HIGHER_IS_BETTER,
        absolute_knots=[(0.0, 0.0), (100.0, 100.0)],
        weights={"absolute": 0.5, "market_history": 0.5},
        components={"market_history": HistoryComponent(None, 0, 0.0)},
    )
    # 仅 absolute:50 + 0.5*1*(60-50) = 55
    assert r.score == pytest.approx(55.0)
    assert r.evidence_coverage == pytest.approx(0.5)
    assert any("无样本" in w for w in r.warnings)


def test_all_scores_finite_and_bounded():
    knots = [(0.0, 0.0), (5.0, 50.0), (12.0, 100.0)]
    for raw in [-100, 0, 0.001, 3, 5, 9, 12, 999]:
        r = combine_hybrid(
            float(raw), direction=Direction.HIGHER_IS_BETTER,
            absolute_knots=knots,
            weights={"absolute": 0.4, "market_history": 0.3,
                     "industry_history": 0.2, "self_history": 0.1},
            components={
                "market_history": _comp(90.0, 1000),
                "industry_history": _comp(10.0, 500),
                "self_history": HistoryComponent(None, 0, 0.0),
            },
        )
        assert math.isfinite(r.score)
        assert 0.0 <= r.score <= 100.0
        assert 0.0 <= r.evidence_coverage <= 1.0


def test_clamp():
    assert clamp(-5) == 0.0
    assert clamp(150) == 100.0
    assert clamp(42) == 42.0
