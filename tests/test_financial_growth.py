"""成长因子 raw（净利同比）单元测试：PIT 时点、口径、缺失语义、fingerprint。

配套 quality 因子测试，验证 growth_ni / growth_rev 从财务利润表取最新已公告
报告期的同比增速，PIT 用公告日（pub_date <= as_of），不污染回测结果。
"""
from datetime import date
from unittest.mock import patch

import pytest

from stockfu.factors.raw.growth import (
    METRIC_NI,
    METRIC_REV,
    compute_growth_ni,
    compute_growth_rev,
)
from stockfu.factors.raw import raw_fingerprint
from stockfu.scoring.contracts import MissingReason
from stockfu.services.financial import FinancialReport

AS_OF = date(2026, 8, 1)


def _rep(year, quarter, pub, ni_yoy=None, rev_yoy=None):
    """构造利润表视图；pub 为 profit 表公告日。"""
    return FinancialReport(
        year=year, quarter=quarter, pub_profit=pub,
        pub_balance=None, pub_cashflow=None,
        net_profit_yoy=ni_yoy, revenue_yoy=rev_yoy)


def _patch_reports(reports: list[FinancialReport]):
    return patch("stockfu.services.financial.financial_reports", return_value=reports)


# ---------------------------------------------------------------- growth_ni


def test_growth_ni_takes_latest_reported_quarter():
    """取最新已公告报告期的净利同比（含季报，及时反映当期成长）。"""
    reports = [
        _rep(2026, 1, date(2026, 4, 25), ni_yoy=42.5),
        _rep(2025, 4, date(2026, 4, 20), ni_yoy=18.0),
    ]
    with _patch_reports(reports):
        obs = compute_growth_ni("600001", AS_OF)
    assert obs.valid is True
    assert obs.raw_value == pytest.approx(42.5)
    assert obs.diagnostics["report"] == "2026Q1"
    assert obs.diagnostics["ni_yoy_pct"] == pytest.approx(42.5, abs=1e-4)
    assert obs.raw_unit == "percent"


def test_growth_ni_pit_uses_pub_date_not_stat_date():
    """PIT：未到公告日的报告期不得被读取（未来函数防护）。"""
    reports = [
        _rep(2026, 2, date(2026, 8, 25), ni_yoy=99.0),   # 8/25 未公告
        _rep(2026, 1, date(2026, 4, 25), ni_yoy=30.0),   # as_of(8/1) 前已公告
    ]
    with _patch_reports(reports):
        obs = compute_growth_ni("600001", AS_OF)
    assert obs.valid is True
    assert obs.raw_value == pytest.approx(30.0)
    assert obs.diagnostics["report"] == "2026Q1"


def test_growth_ni_negative_growth_kept_as_raw():
    """负增速是真实信息，保留原值（不伪造、不钳制）。"""
    reports = [_rep(2025, 4, date(2026, 4, 20), ni_yoy=-35.2)]
    with _patch_reports(reports):
        obs = compute_growth_ni("600001", AS_OF)
    assert obs.valid is True
    assert obs.raw_value == pytest.approx(-35.2)


def test_growth_ni_no_disclosed_missing():
    """无已公告报告 → NOT_DISCLOSED。"""
    reports = [_rep(2026, 2, date(2026, 8, 25), ni_yoy=50.0)]  # 未到公告日
    with _patch_reports(reports):
        obs = compute_growth_ni("600001", AS_OF)
    assert obs.valid is False
    assert obs.missing_reason == MissingReason.NOT_DISCLOSED
    assert obs.raw_value is None


def test_growth_ni_no_reports_missing():
    """完全无财务数据 → NOT_DISCLOSED。"""
    with _patch_reports([]):
        obs = compute_growth_ni("600001", AS_OF)
    assert obs.valid is False
    assert obs.missing_reason == MissingReason.NOT_DISCLOSED


# ---------------------------------------------------------------- growth_rev


def test_growth_rev_takes_latest_reported_quarter():
    """营收同比取最新已公告报告期。"""
    reports = [_rep(2026, 1, date(2026, 4, 25), rev_yoy=25.0)]
    with _patch_reports(reports):
        obs = compute_growth_rev("600001", AS_OF)
    assert obs.valid is True
    assert obs.raw_value == pytest.approx(25.0)
    assert obs.diagnostics["rev_yoy_pct"] == pytest.approx(25.0, abs=1e-4)


def test_growth_rev_missing():
    """营收同比无已公告数据 → NOT_DISCLOSED。"""
    with _patch_reports([]):
        obs = compute_growth_rev("600001", AS_OF)
    assert obs.valid is False
    assert obs.missing_reason == MissingReason.NOT_DISCLOSED


# ---------------------------------------------------------------- fingerprints


def test_growth_ni_fingerprint_matches_v2_registry():
    """growth_ni 的 raw fingerprint 算法名必须与 v2_run.RAW_COMPUTERS 注册一致。"""
    from stockfu.backtest.v2_run import RAW_COMPUTERS
    spec = RAW_COMPUTERS["growth_ni"]
    fp = raw_fingerprint(METRIC_NI, "latest_ni_yoy_pct", {})
    assert spec.algo == "latest_ni_yoy_pct"
    with _patch_reports([_rep(2025, 4, date(2026, 4, 20), ni_yoy=10.0)]):
        obs = compute_growth_ni("600001", AS_OF)
    assert obs.raw_fingerprint == fp


def test_growth_rev_fingerprint_matches_v2_registry():
    """growth_rev 的 raw fingerprint 算法名必须与 v2_run.RAW_COMPUTERS 注册一致。"""
    from stockfu.backtest.v2_run import RAW_COMPUTERS
    spec = RAW_COMPUTERS["growth_rev"]
    fp = raw_fingerprint(METRIC_REV, "latest_rev_yoy_pct", {})
    assert spec.algo == "latest_rev_yoy_pct"
    with _patch_reports([_rep(2025, 4, date(2026, 4, 20), rev_yoy=10.0)]):
        obs = compute_growth_rev("600001", AS_OF)
    assert obs.raw_fingerprint == fp
