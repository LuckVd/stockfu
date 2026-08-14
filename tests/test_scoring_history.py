"""历史状态与评分器单测(对应设计 §22.1 / §22.2)。"""
from __future__ import annotations

from datetime import date

from stockfu.scoring.contracts import RawFactorObservation
from stockfu.scoring.history import HistoryState, compute_sample_dates
from stockfu.scoring.profiles import profile_from_dict
from stockfu.scoring.scorer import FactorScorer

D = date  # 简写


# ---------------------------------------------------------- compute_sample_dates


def test_sample_dates_month_end_and_weekend():
    # 2024-01:31 周三, 02:29 周四(闰), 03:29 周五；补一个后继交易日，
    # 让 03:29 的月末身份由真实后继日期决定，而不是依赖输入末日。
    days = [D(2024, 1, 29), D(2024, 1, 30), D(2024, 1, 31),
            D(2024, 2, 28), D(2024, 2, 29), D(2024, 3, 28), D(2024, 3, 29),
            D(2024, 4, 1)]
    me = compute_sample_dates(days, "month_end")
    assert me == {D(2024, 1, 31), D(2024, 2, 29), D(2024, 3, 29)}
    daily = compute_sample_dates(days, "daily")
    assert daily == set(days)


def test_sample_dates_does_not_treat_prefix_end_as_period_end():
    days = [D(2024, 3, 28), D(2024, 3, 29)]
    assert compute_sample_dates(days, "month_end") == set()
    assert compute_sample_dates(days, "weekend_cross_section") == set()


# --------------------------------------------------------------- self rolling


def test_self_rolling_window():
    h = HistoryState()
    for i, d in enumerate([D(2024, 1, 1), D(2024, 6, 1), D(2025, 1, 1), D(2025, 6, 1)]):
        h.update(d, {"m": {"c1": float(i)}}, {}, "cn_equity",
                 {"m": {"self": True, "market": False, "industry": False}})
    # cutoff=2025-06-01, years=1 → 窗口 (2024-06-02, 2025-06-01]
    s = h.self_samples("m", "c1", D(2025, 6, 1), 1.0)
    assert s == [2.0, 3.0]   # 2025-01-01 与 2025-06-01


def test_market_pool_merges_cross_sections():
    h = HistoryState()
    h.update(D(2024, 1, 31), {"m": {"a": 1.0, "b": 2.0}}, {}, "cn_equity",
             {"m": {"market": True}})
    h.update(D(2024, 2, 28), {"m": {"a": 3.0, "b": 4.0}}, {}, "cn_equity",
             {"m": {"market": True}})
    s = h.market_samples("m", "cn_equity", D(2024, 12, 31), 1.0)
    assert sorted(s) == [1.0, 2.0, 3.0, 4.0]


def test_industry_pool_groups_by_industry():
    h = HistoryState()
    h.update(D(2024, 1, 31), {"m": {"a": 1.0, "b": 2.0, "c": 3.0}},
             {"a": "银行", "b": "银行", "c": "科技"}, "cn_equity",
             {"m": {"industry": True}})
    bank = h.industry_samples("m", "银行", D(2024, 12, 31), 1.0)
    tech = h.industry_samples("m", "科技", D(2024, 12, 31), 1.0)
    assert sorted(bank) == [1.0, 2.0]
    assert tech == [3.0]


# --------------------------------------------------------- checkpoint resume


def test_checkpoint_resume_identical():
    """§14.2:恢复后下一日输出与不间断运行逐位一致。"""
    full = HistoryState()
    for i, d in enumerate([D(2024, 1, 31), D(2024, 2, 28), D(2024, 3, 28)]):
        full.update(d, {"m": {"a": float(i), "b": float(i) * 2}},
                    {"a": "银行", "b": "科技"}, "cn_equity",
                    {"m": {"self": True, "market": True, "industry": True}})

    # 分段:d1,d2 → checkpoint → 恢复 → d3
    seg = HistoryState()
    seg.update(D(2024, 1, 31), {"m": {"a": 0.0, "b": 0.0}}, {"a": "银行", "b": "科技"},
               "cn_equity", {"m": {"self": True, "market": True, "industry": True}})
    seg.update(D(2024, 2, 28), {"m": {"a": 1.0, "b": 2.0}}, {"a": "银行", "b": "科技"},
               "cn_equity", {"m": {"self": True, "market": True, "industry": True}})
    resumed = HistoryState.from_checkpoint(seg.to_checkpoint())
    resumed.update(D(2024, 3, 28), {"m": {"a": 2.0, "b": 4.0}}, {"a": "银行", "b": "科技"},
                   "cn_equity", {"m": {"self": True, "market": True, "industry": True}})

    assert full.to_checkpoint() == resumed.to_checkpoint()
    assert full.state_hash("m", "cn_equity") == resumed.state_hash("m", "cn_equity")


def test_market_write_order_invariant():
    """同日 code 写入顺序不影响 market 池的 ECDF 输入(排序后一致)。"""
    h1 = HistoryState()
    h2 = HistoryState()
    vals = {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0}
    h1.update(D(2024, 1, 31), {"m": vals}, {}, "cn_equity", {"m": {"market": True}})
    # 打乱 dict 顺序重写另一实例(同日同值)
    h2.update(D(2024, 1, 31), {"m": {k: v for k, v in reversed(list(vals.items()))}},
              {}, "cn_equity", {"m": {"market": True}})
    assert sorted(h1.market_samples("m", "cn_equity", D(2024, 12, 31), 1.0)) == \
        sorted(h2.market_samples("m", "cn_equity", D(2024, 12, 31), 1.0))


# ------------------------------------------------------- prefix invariance (core)


def test_prefix_invariance_state():
    """§16.8:延长结束日不改变既有状态。跑到 T1 与 T2(T2>T1),截至 T1 状态逐位相同。"""
    days = [D(2024, 1, 31), D(2024, 2, 28), D(2024, 3, 28), D(2024, 4, 30)]
    series = {D(2024, 1, 31): 1.0, D(2024, 2, 28): 2.0, D(2024, 3, 28): 3.0, D(2024, 4, 30): 4.0}

    def build(until):
        h = HistoryState()
        for d in days:
            if d > until:
                break
            h.update(d, {"m": {"a": series[d]}}, {}, "cn_equity",
                     {"m": {"self": True, "market": True}})
        return h

    h_t1 = build(D(2024, 3, 28))
    h_t2 = build(D(2024, 4, 30))
    # 截至各自 cutoff 的状态摘要一致(只要查询 cutoff<=T1,两者返回相同样本)
    assert h_t1.self_samples("m", "a", D(2024, 3, 28), 1.0) == \
        h_t2.self_samples("m", "a", D(2024, 3, 28), 1.0)


# --------------------------------------------------------- scorer integration


PROFILE_DICT = {
    "profile_id": "test_metric_v1", "version": 1,
    "raw_metric": {"id": "test_metric", "params": {}},
    "direction": "higher_is_better", "raw_unit": "percent",
    "mapping": {"mode": "hybrid", "components": {
        "absolute": {"weight": 0.5, "knots": [[0, 0], [5, 50], [12, 100]]},
        "market_history": {"weight": 0.3, "state": "rolling", "years": 3,
                           "sampling": "month_end_cross_section", "min_observations": 3},
        "self_history": {"weight": 0.2, "state": "rolling", "years": 3,
                         "sampling": "month_end", "min_observations": 2},
    }},
    "formal_requires_mature": True,
}


def test_scorer_end_to_end():
    p = profile_from_dict(PROFILE_DICT)
    scorer = FactorScorer(p)
    h = HistoryState()
    # 灌入历史:market 3 个月末截面 + self per-code
    for i, d in enumerate([D(2023, 11, 30), D(2023, 12, 29), D(2024, 1, 31)]):
        h.update(d, {"test_metric": {"x": float(i), "y": float(i) * 2, "z": 10.0 - i}},
                 {}, "cn_equity",
                 {"test_metric": {"self": True, "market": True, "industry": False}})
    scorer.new_day()
    raw = RawFactorObservation(
        asset_code="x", as_of=D(2024, 2, 1), raw_metric_id="test_metric",
        raw_value=6.0, raw_unit="percent", source_max_date=D(2024, 1, 31),
        available_at=D(2024, 2, 1), valid=True, raw_fingerprint="rawfp")
    fs = scorer.score(raw, h, industry=None, market_scope="cn_equity", cutoff=D(2024, 1, 31))
    assert 0.0 <= fs.score <= 100.0
    assert 0.0 <= fs.evidence_coverage <= 1.0
    assert fs.profile_id == "test_metric_v1"
    assert fs.reference_cutoff == D(2024, 1, 31)
    assert fs.absolute_score == 50.0 + (6.0 - 5.0) / (12.0 - 5.0) * 50.0   # ≈57.14
    assert fs.maturity is not None


def test_scorer_missing_raw_is_50():
    p = profile_from_dict(PROFILE_DICT)
    scorer = FactorScorer(p)
    h = HistoryState()
    scorer.new_day()
    raw = RawFactorObservation(
        asset_code="x", as_of=D(2024, 2, 1), raw_metric_id="test_metric",
        raw_value=None, raw_unit="percent", source_max_date=D(2024, 1, 31),
        available_at=D(2024, 2, 1), valid=False, raw_fingerprint="rawfp")
    fs = scorer.score(raw, h, industry=None, market_scope="cn_equity", cutoff=D(2024, 1, 31))
    assert fs.score == 50.0
    assert fs.evidence_coverage == 0.0
