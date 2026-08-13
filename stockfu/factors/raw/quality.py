"""quality:质量因子 raw 计算器（财务三表 PIT，2026-08 新增）。

依据 docs/SPECS/style-factor-research-2026.md：
- 简单历史 ROE 因子弱，"稳定性/预期"改进版才显著（招商 PB-ROE 系列、华证新质量因子）；
  故 quality_roe = 最新已公告 ROE 水平 − 近 N 个完整年度 ROE 的标准差（水平+稳定一刀）。
- 毛利率（定价能力）与资产负债率（财务安全）是质量因子的常见组成（华证/QMJ 口径）。

PIT：全部字段取 pub_date(=NOTICE_DATE 公告日) <= as_of 的最新已公告报告期
（services/financial.py，禁止用 stat_date 过滤 → 无未来函数）。

本层只产 raw（原始值），direction/映射由 profile 层决定。
"""
from __future__ import annotations

import statistics
from datetime import date

from stockfu.factors.raw import raw_fingerprint
from stockfu.scoring.contracts import MissingReason, RawFactorObservation

METRIC_ID = "quality_roe"
METRIC_GROSS_MARGIN = "gross_margin"
METRIC_LEVERAGE = "leverage"


def _missing(code: str, as_of: date, metric: str, fp: str, reason: MissingReason,
             diag: dict) -> RawFactorObservation:
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=metric,
        raw_value=None, raw_unit="percent", source_max_date=as_of,
        available_at=as_of, valid=False, missing_reason=reason,
        raw_fingerprint=fp, diagnostics=diag)


def compute_quality_roe(code: str, as_of: date, years: int = 5,
                        min_years: int = 3) -> RawFactorObservation:
    """ROE 质量 = 最新完整年度 ROE(%) − 近 years 个完整年度 ROE 的标准差(%)。

    水平与稳定性**都用年报（quarter=4）口径**，避免单季 ROE 与年度 ROE 直接相减
    的季节性偏误（Q1 单季 ROE 系统性低于全年，混用会把好公司误判为负质量）。
    年度样本 < min_years 时只给水平（无波动惩罚，lookback 标注实际样本数，
    评分层 maturity 会收缩）；完全无年报数据 → NOT_DISCLOSED。
    """
    years = int(years)
    min_years = int(min_years)
    if years <= 0 or min_years <= 0 or min_years > years:
        raise ValueError("quality_roe 的 years/min_years 参数无效(0<min_years<=years)")
    fp = raw_fingerprint(
        METRIC_ID, "roe_level_minus_annual_std",
        {"years": years, "min_years": min_years, "basis": "annual"},
    )
    from stockfu.services.financial import financial_reports_before

    annual = financial_reports_before(code, as_of, table="profit", quarters=(4,))
    annual_roes = [r.roe_avg for r in annual
                   if r.roe_avg is not None and r.pub_date <= as_of]
    if not annual_roes:
        return _missing(code, as_of, METRIC_ID, fp, MissingReason.NOT_DISCLOSED,
                        {"pub_latest": None})
    # 只取最近 years 个完整年度（序列按 pub_date 升序，尾部即最新）
    annual_roes = annual_roes[-years:]
    level = annual_roes[-1]
    diag = {
        "roe_level": round(level, 4),
        "report": f"{annual[-1].year}Q4",
        "pub_date": annual[-1].pub_date.isoformat(),
        "annual_roes": [round(x, 4) for x in annual_roes],
    }
    if len(annual_roes) >= min_years:
        std = statistics.pstdev(annual_roes)      # 总体标准差（样本即"过去 N 年"全集）
        diag["roe_std"] = round(std, 4)
        diag["n_annual"] = len(annual_roes)
        return RawFactorObservation(
            asset_code=code, as_of=as_of, raw_metric_id=METRIC_ID,
            raw_value=float(level - std), raw_unit="percent",
            source_max_date=as_of, available_at=as_of, valid=True,
            raw_fingerprint=fp, lookback_observations=len(annual_roes),
            diagnostics=diag)
    # 年度样本不足 min_years：仅水平，明确标注样本数（评分层 maturity 会收缩）
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=METRIC_ID,
        raw_value=float(level), raw_unit="percent",
        source_max_date=as_of, available_at=as_of, valid=True,
        raw_fingerprint=fp, lookback_observations=len(annual_roes), diagnostics=diag)


def compute_gross_margin(code: str, as_of: date) -> RawFactorObservation:
    """销售毛利率（%）= 最新已公告报告期 XSMLL。可负（毛利为负是真实信息，不伪造）。"""
    fp = raw_fingerprint(METRIC_GROSS_MARGIN, "latest_gp_margin_pct", {})
    from stockfu.services.financial import latest_financial_report

    latest = latest_financial_report(code, as_of)
    if latest is None or latest.gp_margin is None:
        return _missing(code, as_of, METRIC_GROSS_MARGIN, fp,
                        MissingReason.NOT_DISCLOSED, {"pub_latest": None})
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=METRIC_GROSS_MARGIN,
        raw_value=float(latest.gp_margin), raw_unit="percent",
        source_max_date=as_of, available_at=as_of, valid=True, raw_fingerprint=fp,
        diagnostics={"report": f"{latest.year}Q{latest.quarter}",
                     "pub_date": latest.pub_date.isoformat(),
                     "gp_margin_pct": round(latest.gp_margin, 4)})


def compute_leverage(code: str, as_of: date) -> RawFactorObservation:
    """资产负债率（%）= 最新已公告报告期 LIABILITY_TO_ASSET。越低越好由 profile 定方向。

    <=0 视为异常缺失（负债率不可能是 0 以下；资不抵债 >100 保留真实值）。
    """
    fp = raw_fingerprint(METRIC_LEVERAGE, "latest_liability_to_asset_pct", {})
    from stockfu.services.financial import latest_financial_report

    latest = latest_financial_report(code, as_of)
    if latest is None or latest.liability_to_asset is None:
        return _missing(code, as_of, METRIC_LEVERAGE, fp,
                        MissingReason.NOT_DISCLOSED, {"pub_latest": None})
    val = latest.liability_to_asset
    if val <= 0:
        return _missing(code, as_of, METRIC_LEVERAGE, fp,
                        MissingReason.NONPOSITIVE_DENOMINATOR,
                        {"liability_to_asset": val})
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=METRIC_LEVERAGE,
        raw_value=float(val), raw_unit="percent",
        source_max_date=as_of, available_at=as_of, valid=True, raw_fingerprint=fp,
        diagnostics={"report": f"{latest.year}Q{latest.quarter}",
                     "pub_date": latest.pub_date.isoformat(),
                     "liability_to_asset_pct": round(val, 4)})
