"""质量因子 raw（财务三表 PIT）单元测试：PIT 时点、口径、缺失语义、provider 一致性。"""
from datetime import date
from unittest.mock import patch

import pytest

from stockfu.factors.raw.quality import (
    compute_asset_growth,
    compute_cash_quality,
    compute_gpoa,
    compute_gross_margin,
    compute_leverage,
    compute_net_margin,
    compute_quality_roe,
)
from stockfu.scoring.contracts import MissingReason
from stockfu.services.financial import FinancialReport

AS_OF = date(2026, 8, 1)


def _rep(year, quarter, pub, roe=None, gp=None, lia=None, stat=None,
         np_=None, rev=None, ta=None, eq=None, nco=None):
    """构造三表合并视图；pub 为各表公告日（缺省全同）。"""
    return FinancialReport(
        year=year, quarter=quarter, stat_date=stat,
        pub_profit=pub, pub_balance=pub, pub_cashflow=pub,
        roe_avg=roe, gp_margin=gp, liability_to_asset=lia,
        net_profit=np_, revenue=rev, total_assets=ta, equity=eq,
        net_cash_oper=nco)


def _annual_roes(roes: list[float], base_year: int = 2021) -> list[FinancialReport]:
    """构造连续年度 ROE 序列（Q4 报告期），pub_date 按披露惯例 4 月末。

    返回按 (year, quarter) **降序**（最新在前），与 services.financial 契约一致。
    """
    return [
        _rep(y, 4, date(y + 1, 4, 30), roe=r) for y, r in zip(
            range(base_year, base_year + len(roes)), roes)
    ][::-1]


def _patch_reports(reports: list[FinancialReport]):
    return patch("stockfu.services.financial.financial_reports", return_value=reports)


# ---------------------------------------------------------------- quality_roe


def test_quality_roe_level_minus_std_annual_basis():
    reports = _annual_roes([20.0, 22.0, 24.0, 26.0, 28.0])
    with _patch_reports(reports):
        obs = compute_quality_roe("600001", AS_OF)
    # 水平=最新年报 28.0；std=pstdev([20,22,24,26,28])=√8≈2.828
    assert obs.valid is True
    assert obs.raw_value == pytest.approx(28.0 - 2.8284, abs=1e-3)
    assert obs.diagnostics["report"] == "2025Q4"
    assert obs.lookback_observations == 5
    assert obs.raw_unit == "percent"


def test_quality_roe_pit_uses_pub_date_not_stat_date():
    """PIT：pub_date > as_of 的最新财报不得参与（模拟 2026-08-01 尚未公告半年报）。"""
    reports = _annual_roes([20.0, 22.0, 24.0, 26.0])
    reports.append(_rep(2026, 2, date(2026, 8, 30), roe=99.0))  # 未来公告
    with _patch_reports(reports):
        obs = compute_quality_roe("600001", AS_OF)
    assert obs.valid is True
    assert obs.diagnostics["report"] == "2024Q4"
    assert 99.0 not in obs.diagnostics["annual_roes"]
    # 只取最近 years 个年度
    assert len(obs.diagnostics["annual_roes"]) == 4


def test_quality_roe_insufficient_annual_gives_level_without_penalty():
    reports = _annual_roes([25.0, 26.0])   # 2 个年度 < min_years=3
    with _patch_reports(reports):
        obs = compute_quality_roe("600001", AS_OF)
    assert obs.valid is True
    assert obs.raw_value == pytest.approx(26.0)
    assert obs.lookback_observations == 2


def test_quality_roe_no_reports_missing():
    with _patch_reports([]):
        obs = compute_quality_roe("600001", AS_OF)
    assert obs.valid is False
    assert obs.raw_value is None
    assert obs.missing_reason == MissingReason.NOT_DISCLOSED


def test_quality_roe_params_validation():
    with pytest.raises(ValueError):
        compute_quality_roe("600001", AS_OF, years=2, min_years=3)


# ---------------------------------------------------------------- gross_margin / leverage


def test_gross_margin_latest_report():
    reports = [
        _rep(2025, 4, date(2026, 4, 30), roe=20.0, gp=45.5),
        _rep(2026, 2, date(2026, 8, 30), roe=10.0, gp=99.9),  # 未来公告
    ]
    with _patch_reports(reports):
        obs = compute_gross_margin("600001", AS_OF)
    assert obs.valid is True
    assert obs.raw_value == pytest.approx(45.5)
    assert obs.diagnostics["report"] == "2025Q4"


def test_leverage_lower_value_kept_as_raw():
    reports = [_rep(2026, 1, date(2026, 4, 30), lia=35.7)]
    with _patch_reports(reports):
        obs = compute_leverage("600001", AS_OF)
    assert obs.valid is True
    assert obs.raw_value == pytest.approx(35.7)


def test_leverage_nonpositive_missing():
    reports = [_rep(2026, 1, date(2026, 4, 30), lia=0.0)]
    with _patch_reports(reports):
        obs = compute_leverage("600001", AS_OF)
    assert obs.valid is False
    assert obs.missing_reason == MissingReason.NONPOSITIVE_DENOMINATOR


# ---------------------------------------------------------------- 新增三表因子


def test_gpoa_annual_basis_and_cross_table_pit():
    """GPOA 用年报口径；balance 公告日晚于 profit 时该报告期不可见（保守缺失）。"""
    late_bal = FinancialReport(
        year=2025, quarter=4, stat_date=None,
        pub_profit=date(2026, 4, 17), pub_balance=date(2026, 4, 25),
        pub_cashflow=date(2026, 4, 25),
        gp_margin=90.0, revenue=1000.0, total_assets=2000.0)
    # as_of 在 balance 公告前：字段级不可见 → 无可见年报 → 缺失
    with _patch_reports([late_bal]):
        obs = compute_gpoa("600001", date(2026, 4, 20))
    assert obs.valid is False
    assert obs.missing_reason == MissingReason.NOT_DISCLOSED
    # balance 公告后：可见，GPOA = 1000×90%/2000 = 45%
    with _patch_reports([late_bal]):
        obs = compute_gpoa("600001", date(2026, 4, 26))
    assert obs.valid is True
    assert obs.raw_value == pytest.approx(45.0)


def test_net_margin_annual():
    reports = [_rep(2025, 4, date(2026, 4, 30), np_=30.0, rev=100.0)]
    with _patch_reports(reports):
        obs = compute_net_margin("600001", AS_OF)
    assert obs.valid is True
    assert obs.raw_value == pytest.approx(30.0)


def test_cash_quality_positive_and_nonpositive_net_profit():
    reports = [_rep(2025, 4, date(2026, 4, 30), np_=80.0, nco=120.0)]
    with _patch_reports(reports):
        obs = compute_cash_quality("600001", AS_OF)
    assert obs.valid is True
    assert obs.raw_value == pytest.approx(150.0)
    # 净利 <= 0：比值无意义 → 缺失
    reports2 = [_rep(2025, 4, date(2026, 4, 30), np_=-10.0, nco=5.0)]
    with _patch_reports(reports2):
        obs = compute_cash_quality("600001", AS_OF)
    assert obs.valid is False
    assert obs.missing_reason == MissingReason.NONPOSITIVE_DENOMINATOR


def test_asset_growth_needs_two_annual_balance_reports():
    reports = [
        _rep(2025, 4, date(2026, 4, 30), ta=130.0),
        _rep(2024, 4, date(2025, 4, 30), ta=100.0),
    ]
    with _patch_reports(reports):
        obs = compute_asset_growth("600001", AS_OF)
    assert obs.valid is True
    assert obs.raw_value == pytest.approx(30.0)
    with _patch_reports(reports[:1]):
        obs = compute_asset_growth("600001", AS_OF)
    assert obs.valid is False
    assert obs.missing_reason == MissingReason.INSUFFICIENT_SAMPLES


# ---------------------------------------------------------------- 指纹与一致性


def test_quality_fingerprints_stable_per_params():
    reports = _annual_roes([20.0, 22.0, 24.0])
    with _patch_reports(reports):
        a = compute_quality_roe("600001", AS_OF)
        b = compute_quality_roe("600001", AS_OF)
    assert a.raw_fingerprint == b.raw_fingerprint
    with _patch_reports(reports):
        c = compute_quality_roe("600001", AS_OF, years=4)
    assert c.raw_fingerprint != a.raw_fingerprint


def test_quality_fingerprint_algo_matches_v2_registry():
    """RAW_COMPUTERS 登记的 algo 必须与 raw_fingerprint 内一致（v2_run 注册校验依赖）。"""
    from stockfu.backtest.v2_run import RAW_COMPUTERS
    for metric, spec in [
        ("quality_roe", "roe_level_minus_annual_std"),
        ("gross_margin", "latest_gp_margin_pct"),
        ("leverage", "latest_liability_to_asset_pct"),
        ("gpoa", "gross_profit_over_assets_pct"),
        ("net_margin", "net_profit_over_revenue_pct"),
        ("cash_quality", "ocf_over_net_profit_pct"),
        ("asset_growth", "asset_growth_yoy_pct"),
    ]:
        assert RAW_COMPUTERS[metric].algo == spec
