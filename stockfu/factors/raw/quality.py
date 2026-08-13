"""quality:质量因子 raw 计算器（财务三表 PIT，2026-08 新增）。

依据 docs/SPECS/style-factor-research-2026.md 与 QMJ(Asness 2019)/华证新质量因子：
- quality_roe（已实现）：最新年报 ROE − pstdev(近 N 年年度 ROE)——水平+稳定性。
- gpoa：毛利/总资产（Novy-Marx 2013 毛利溢价核心，不受杠杆扭曲）。
- net_margin：归母净利/营收（华证"定价能力"维度）。
- cash_quality：经营现金流/归母净利（盈余质量，识别应收堆砌的纸面利润）。
- leverage（已实现）：资产负债率（QMJ 安全性维度）。
- asset_growth：总资产同比增速（QMJ 成长性维度，**负向**——高资产扩张=乱花钱）。

PIT：字段级可见性——每个字段所在表（profit/balance/cashflow）的公告日都须
<= as_of（services/financial.py FinancialReport.visible）。跨表合成因子要求
全部来源字段可见，绝不混用不同报告期拼凑（三表公告日可能不同）。

本层只产 raw（原始值），direction/映射由 profile 层决定。
"""
from __future__ import annotations

import statistics
from datetime import date

from stockfu.factors.raw import raw_fingerprint
from stockfu.scoring.contracts import MissingReason, RawFactorObservation

METRIC_ID = "quality_roe"
METRIC_GROSS_MARGIN = "gross_margin"
METRIC_GPOA = "gpoa"
METRIC_NET_MARGIN = "net_margin"
METRIC_CASH_QUALITY = "cash_quality"
METRIC_LEVERAGE = "leverage"
METRIC_ASSET_GROWTH = "asset_growth"


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
        {"years": years, "min_years": min_years},
    )
    from stockfu.services.financial import financial_reports_before

    annual = financial_reports_before(code, as_of, table="profit", quarters=(4,))
    annual_roes = [r.roe_avg for r in annual
                   if r.roe_avg is not None]
    if not annual_roes:
        return _missing(code, as_of, METRIC_ID, fp, MissingReason.NOT_DISCLOSED,
                        {"pub_latest": None})
    annual_roes = annual_roes[:years]        # 降序，头部即最新
    level = annual_roes[0]
    latest = annual[0]
    diag = {
        "roe_level": round(level, 4),
        "report": f"{latest.year}Q4",
        "pub_date": latest.pub_date.isoformat() if latest.pub_date else None,
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

    latest = latest_financial_report(code, as_of, require=("gp_margin",))
    if latest is None or latest.gp_margin is None:
        return _missing(code, as_of, METRIC_GROSS_MARGIN, fp,
                        MissingReason.NOT_DISCLOSED, {"pub_latest": None})
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=METRIC_GROSS_MARGIN,
        raw_value=float(latest.gp_margin), raw_unit="percent",
        source_max_date=as_of, available_at=as_of, valid=True, raw_fingerprint=fp,
        diagnostics={"report": f"{latest.year}Q{latest.quarter}",
                     "pub_date": latest.pub_date.isoformat() if latest.pub_date else None,
                     "gp_margin_pct": round(latest.gp_margin, 4)})


def compute_leverage(code: str, as_of: date) -> RawFactorObservation:
    """资产负债率（%）= 最新已公告报告期 LIABILITY_TO_ASSET。越低越好由 profile 定方向。

    <=0 视为异常缺失（负债率不可能是 0 以下；资不抵债 >100 保留真实值）。
    """
    fp = raw_fingerprint(METRIC_LEVERAGE, "latest_liability_to_asset_pct", {})
    from stockfu.services.financial import latest_financial_report

    latest = latest_financial_report(code, as_of, require=("liability_to_asset",))
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
                     "pub_date": latest.pub_date.isoformat() if latest.pub_date else None,
                     "liability_to_asset_pct": round(val, 4)})


def compute_gpoa(code: str, as_of: date) -> RawFactorObservation:
    """GPOA（%） = 毛利/总资产 = 年报 revenue×gp_margin% ÷ 年报 total_assets × 100。

    Novy-Marx(2013) 毛利溢价核心：反映资本效率，不受财务杠杆扭曲（对比 ROE）。
    **年报口径**（与 quality_roe 同理）：单季营收/总资产有季节性，混用会误判；
    需 profit（revenue/gp_margin）与 balance（total_assets）**均已公告**的年报。
    """
    fp = raw_fingerprint(METRIC_GPOA, "gross_profit_over_assets_pct", {})
    from stockfu.services.financial import latest_financial_report

    latest = latest_financial_report(
        code, as_of, require=("revenue", "gp_margin", "total_assets"),
        quarters=(4,))
    if latest is None or None in (latest.revenue, latest.gp_margin, latest.total_assets):
        return _missing(code, as_of, METRIC_GPOA, fp,
                        MissingReason.NOT_DISCLOSED, {"pub_latest": None})
    gross = latest.revenue * latest.gp_margin / 100.0
    if latest.total_assets <= 0:
        return _missing(code, as_of, METRIC_GPOA, fp,
                        MissingReason.NONPOSITIVE_DENOMINATOR,
                        {"total_assets": latest.total_assets})
    raw = gross / latest.total_assets * 100.0
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=METRIC_GPOA,
        raw_value=float(raw), raw_unit="percent",
        source_max_date=as_of, available_at=as_of, valid=True, raw_fingerprint=fp,
        diagnostics={"report": f"{latest.year}Q{latest.quarter}",
                     "pub_date": latest.pub_date.isoformat() if latest.pub_date else None,
                     "gpoa_pct": round(raw, 4)})


def compute_net_margin(code: str, as_of: date) -> RawFactorObservation:
    """净利率（%） = 年报归母净利 ÷ 年报营收 × 100（华证"定价能力"维度）。

    负净利保留负值（真实信息）；营收 <=0 视为异常缺失。年报口径避免季度季节性。
    """
    fp = raw_fingerprint(METRIC_NET_MARGIN, "net_profit_over_revenue_pct", {})
    from stockfu.services.financial import latest_financial_report

    latest = latest_financial_report(
        code, as_of, require=("net_profit", "revenue"), quarters=(4,))
    if latest is None or None in (latest.net_profit, latest.revenue):
        return _missing(code, as_of, METRIC_NET_MARGIN, fp,
                        MissingReason.NOT_DISCLOSED, {"pub_latest": None})
    if latest.revenue <= 0:
        return _missing(code, as_of, METRIC_NET_MARGIN, fp,
                        MissingReason.NONPOSITIVE_DENOMINATOR,
                        {"revenue": latest.revenue})
    raw = latest.net_profit / latest.revenue * 100.0
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=METRIC_NET_MARGIN,
        raw_value=float(raw), raw_unit="percent",
        source_max_date=as_of, available_at=as_of, valid=True, raw_fingerprint=fp,
        diagnostics={"report": f"{latest.year}Q{latest.quarter}",
                     "pub_date": latest.pub_date.isoformat() if latest.pub_date else None,
                     "net_margin_pct": round(raw, 4)})


def compute_cash_quality(code: str, as_of: date) -> RawFactorObservation:
    """现金流质量（%） = 年报经营现金流净额 ÷ 年报归母净利 × 100（盈余质量）。

    >100：利润有真实现金支撑；<100 或负：利润含应收/存货等应计成分（Sloan 应计
    异象逻辑）。净利 <=0 时比值无意义 → 缺失（亏损公司的现金流质量另行解释）。
    年报口径避免季度季节性。
    """
    fp = raw_fingerprint(METRIC_CASH_QUALITY, "ocf_over_net_profit_pct", {})
    from stockfu.services.financial import latest_financial_report

    latest = latest_financial_report(
        code, as_of, require=("net_cash_oper", "net_profit"), quarters=(4,))
    if latest is None or None in (latest.net_cash_oper, latest.net_profit):
        return _missing(code, as_of, METRIC_CASH_QUALITY, fp,
                        MissingReason.NOT_DISCLOSED, {"pub_latest": None})
    if latest.net_profit <= 0:
        return _missing(code, as_of, METRIC_CASH_QUALITY, fp,
                        MissingReason.NONPOSITIVE_DENOMINATOR,
                        {"net_profit": latest.net_profit})
    raw = latest.net_cash_oper / latest.net_profit * 100.0
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=METRIC_CASH_QUALITY,
        raw_value=float(raw), raw_unit="percent",
        source_max_date=as_of, available_at=as_of, valid=True, raw_fingerprint=fp,
        diagnostics={"report": f"{latest.year}Q{latest.quarter}",
                     "pub_date": latest.pub_date.isoformat() if latest.pub_date else None,
                     "cash_quality_pct": round(raw, 4)})


def compute_asset_growth(code: str, as_of: date) -> RawFactorObservation:
    """总资产同比增速（%） = 最新年报 total_assets / 一年前年报 − 1 × 100。

    QMJ 成长性维度，**负向**（高资产扩张通常伴随过度投资/商誉风险，质量差）。
    需要连续两年年报 balance 已公告；次年缺失 → NOT_DISCLOSED。
    """
    fp = raw_fingerprint(METRIC_ASSET_GROWTH, "asset_growth_yoy_pct", {})
    from stockfu.services.financial import financial_reports_before

    annual = financial_reports_before(code, as_of, table="balance", quarters=(4,))
    if len(annual) < 2:
        return _missing(code, as_of, METRIC_ASSET_GROWTH, fp,
                        MissingReason.INSUFFICIENT_SAMPLES,
                        {"n_annual": len(annual)})
    now = annual[0]
    prev = next((r for r in annual if r.year == now.year - 1), None)
    if prev is None or prev.total_assets is None or prev.total_assets <= 0:
        return _missing(code, as_of, METRIC_ASSET_GROWTH, fp,
                        MissingReason.INSUFFICIENT_SAMPLES,
                        {"n_annual": len(annual), "now_year": now.year})
    raw = (now.total_assets / prev.total_assets - 1.0) * 100.0
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=METRIC_ASSET_GROWTH,
        raw_value=float(raw), raw_unit="percent",
        source_max_date=as_of, available_at=as_of, valid=True, raw_fingerprint=fp,
        diagnostics={"report": f"{now.year}Q4", "prev_year": prev.year,
                     "pub_date": now.pub_date.isoformat() if now.pub_date else None,
                     "asset_growth_pct": round(raw, 4)})
