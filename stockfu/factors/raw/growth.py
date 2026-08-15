"""growth:成长/盈利增速 raw 计算器（财务三表 PIT，2026-08 新增）。

依据 docs/SPECS/style-factor-research-2026.md（成长因子 2025–2026 有效但分化大，
需作为多因子/进攻策略的一极）与金融数据设计（financial-data-design.md §2.3）：

- `growth_ni`（已实现）：最新已公告报告期**归母净利同比**（%），SJLTZ。
- `growth_rev`：最新已公告报告期**营收同比**（%），YSTZ。

成长口径：净利同比优先取**最新已公告报告期**（含季报，能及时反映当期盈利变化），
报告期降序取第一条且字段已公告（pub_date <= as_of）。负增速保留真实值（亏损是信息），
异常大绝对值（>±1000%）保留但建议 profile 裁剪。

PIT：字段级可见性——net_profit_yoy / revenue_yoy 所在表 financial_profit 的公告日
须 <= as_of（services/financial.py FinancialReport.visible）。本层只产 raw，
direction/映射由 profile 层决定。
"""
from __future__ import annotations

from datetime import date

from stockfu.factors.raw import raw_fingerprint
from stockfu.scoring.contracts import MissingReason, RawFactorObservation

# 净利同比（%），归母净利润同比增长 SJLTZ——盈利成长的进攻核心
METRIC_NI = "growth_ni"
# 营收同比（%），营业总收入同比增长 YSTZ——收入增长的进攻支撑
METRIC_REV = "growth_rev"
# 盈利加速（利润动量）%：最新已公告报告期净利同比 − 同报告期一年前的净利同比。
# 正值 = 盈利在加速改善（动态进攻），区别于静态增速水平（growth_ni）。
METRIC_ACCEL = "growth_accel"


def _missing(code: str, as_of: date, metric: str, fp: str, reason: MissingReason,
             diag: dict) -> RawFactorObservation:
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=metric,
        raw_value=None, raw_unit="percent", source_max_date=as_of,
        available_at=as_of, valid=False, missing_reason=reason,
        raw_fingerprint=fp, diagnostics=diag)


def _latest_growth(code: str, as_of: date, *, metric: str, algo: str,
                   field: str, fn_label: str) -> RawFactorObservation:
    """取最新已公告报告期的同比增速（%）。

    field：FinancialReport 上的字段名（net_profit_yoy / revenue_yoy）。
    取报告期降序第一个字段可见的行（含季报，及时反映当期成长）。
    """
    fp = raw_fingerprint(metric, algo, {})
    from stockfu.services.financial import latest_financial_report

    latest = latest_financial_report(code, as_of, require=(field,))
    if latest is None or getattr(latest, field) is None:
        return _missing(code, as_of, metric, fp, MissingReason.NOT_DISCLOSED,
                        {"pub_latest": None})
    val = getattr(latest, field)
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=metric,
        raw_value=float(val), raw_unit="percent",
        source_max_date=as_of, available_at=as_of, valid=True,
        raw_fingerprint=fp,
        diagnostics={"report": f"{latest.year}Q{latest.quarter}",
                     "pub_date": latest.pub_date.isoformat() if latest.pub_date else None,
                     fn_label: round(val, 4)})


def compute_growth_ni(code: str, as_of: date) -> RawFactorObservation:
    """归母净利同比（%）= 最新已公告报告期 SJLTZ。负值保留（亏损是真实信息）。"""
    return _latest_growth(
        code, as_of, metric=METRIC_NI, algo="latest_ni_yoy_pct",
        field="net_profit_yoy", fn_label="ni_yoy_pct")


def compute_growth_rev(code: str, as_of: date) -> RawFactorObservation:
    """营收同比（%）= 最新已公告报告期 YSTZ。负值保留（营收收缩是真实信息）。"""
    return _latest_growth(
        code, as_of, metric=METRIC_REV, algo="latest_rev_yoy_pct",
        field="revenue_yoy", fn_label="rev_yoy_pct")


def compute_growth_accel(code: str, as_of: date) -> RawFactorObservation:
    """盈利加速（利润动量，%）= 最新已公告报告期净利同比 − 同报告期一年前的净利同比。

    accel = net_profit_yoy(q_now) − net_profit_yoy(q_same_last_year)
    正值 = 盈利同比在加速改善（动态进攻），区别于静态增速水平（growth_ni）。
    要求存在最新已公告报告期及其一年前同一报告期的净利同比；任一缺失 → NOT_DISCLOSED。
    同报告期对比（同季度 yoy）消除季节性，是"利润在变好"的动量信号。
    """
    fp = raw_fingerprint(METRIC_ACCEL, "ni_yoy_accel_latest_vs_yoy", {})
    from stockfu.services.financial import financial_reports

    reported = financial_reports(code, as_of) or []
    # 字段级可见性：只保留 net_profit_yoy 已在 as_of 前公告的报告期
    visible = [r for r in reported if r.visible("net_profit_yoy", as_of)]
    if not visible:
        return _missing(code, as_of, METRIC_ACCEL, fp, MissingReason.NOT_DISCLOSED,
                        {"pub_latest": None})
    now = visible[0]  # 最新已公告报告期
    if now.net_profit_yoy is None:
        return _missing(code, as_of, METRIC_ACCEL, fp, MissingReason.NOT_DISCLOSED,
                        {"report": f"{now.year}Q{now.quarter}"})
    # 找去年同一报告期
    prev = next((r for r in visible
                 if r.year == now.year - 1 and r.quarter == now.quarter), None)
    if prev is None or prev.net_profit_yoy is None:
        return _missing(code, as_of, METRIC_ACCEL, fp, MissingReason.INSUFFICIENT_SAMPLES,
                        {"report": f"{now.year}Q{now.quarter}",
                         "prev_missing": True})
    accel = now.net_profit_yoy - prev.net_profit_yoy
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=METRIC_ACCEL,
        raw_value=float(accel), raw_unit="percent",
        source_max_date=as_of, available_at=as_of, valid=True,
        raw_fingerprint=fp,
        diagnostics={"report": f"{now.year}Q{now.quarter}",
                     "ni_yoy_now": round(now.net_profit_yoy, 4),
                     "ni_yoy_prev": round(prev.net_profit_yoy, 4)
                     if prev.net_profit_yoy is not None else None,
                     "accel_pct": round(accel, 4),
                     "pub_date": now.pub_date.isoformat() if now.pub_date else None})
