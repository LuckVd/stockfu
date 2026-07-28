"""分红数据通用解析工具。

核心逻辑借鉴自 daily_stock_analysis/data_provider/fundamental_adapter.py (MIT)：
- 用关键词映射容错定位中文列名（不同 akshare 端点列名不一致）；
- 先取「每股派息」数值列，取不到再从「10 股派 X 元」方案文本解析；
- 按 (除权日, 每股派息) 去重，近 365 天求和算 TTM 每股派息。
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Optional

import pandas as pd

from stockfu.data.base import DividendEventDTO, DividendMetric

_KW: dict[str, list[str]] = {
    "per_share": ["每股派息", "每股现金红利", "每股分红", "每股派现",
                  "派现(元/股)", "派息(元/股)", "税前派息(元/股)",
                  "现金分红(税前)", "每股派息(税前)", "现金分红"],
    "plan_text": ["分配方案", "分红方案", "实施方案", "派息方案",
                  "方案", "预案", "方案说明"],
    "ex_dividend_date": ["除权除息日", "除息日", "除权日", "除权除息", "除息日期"],
    "record_date": ["股权登记日", "登记日"],
    "announce_date": ["公告日期", "公告日", "实施公告日", "预案公告日"],
}


def safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            f = float(value)
        else:
            s = str(value).strip().replace(",", "").replace("%", "").replace("元", "")
            if not s or s in ("-", "--"):
                return None
            f = float(s)
    except (TypeError, ValueError):
        return None
    if f != f or f == float('inf') or f == float('-inf'):   # pandas NaN/inf 当空，避免污染除法
        return None
    return f


def safe_str(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _pick(row: pd.Series, keys: list[str]) -> Any:
    """返回第一个列名含任一关键词且非空的值。"""
    for col in row.index:
        col_s = str(col)
        if any(k in col_s for k in keys):
            val = row.get(col)
            if val is not None and str(val).strip() not in ("", "-", "nan", "None"):
                return val
    return None


def _parse_plan_to_per_share(text: str) -> Optional[float]:
    """从「10 股派 X 元」「每股派 X 元」方案文本解析每股派息。"""
    t = safe_str(text)
    if not t:
        return None
    if "税后" in t and "税前" not in t and "含税" not in t:
        return None  # 只保留税前口径
    for pat in (r"(?:每)?\s*10\s*股?\s*派(?:发)?\s*([0-9]+(?:\.[0-9]+)?)",
                r"10\s*派\s*([0-9]+(?:\.[0-9]+)?)"):
        m = re.search(pat, t)
        if m:
            v = safe_float(m.group(1))
            if v and v > 0:
                return round(v / 10.0, 6)
    m = re.search(r"每\s*股\s*派(?:发)?\s*([0-9]+(?:\.[0-9]+)?)", t)
    if m:
        v = safe_float(m.group(1))
        if v and v > 0:
            return v
    return None


def _extract_per_share(row: pd.Series) -> Optional[float]:
    plan = safe_str(_pick(row, _KW["plan_text"]))
    direct = safe_float(_pick(row, _KW["per_share"]))
    if direct and direct > 0:
        return round(direct, 6)
    return _parse_plan_to_per_share(plan)


def _norm_code(raw: Any) -> str:
    s = safe_str(raw).upper()
    if "." in s:
        s = s.split(".", 1)[0]
    return re.sub(r"^(SH|SZ|BJ|HK)", "", s)


def _to_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    try:
        ts = pd.to_datetime(value)
    except Exception:
        return None
    if pd.isna(ts):
        return None
    try:
        return ts.date()
    except Exception:
        return None


def _filter_rows(df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    code_cols = [c for c in df.columns
                 if any(k in str(c) for k in ("代码", "股票代码", "证券代码", "symbol", "ts_code"))]
    if not code_cols:
        return df  # 单只查询，整表即目标
    target = _norm_code(stock_code)
    for col in code_cols:
        try:
            mask = df[col].astype(str).map(_norm_code) == target
            if mask.any():
                return df[mask]
        except Exception:
            continue
    return pd.DataFrame()


def _latest_annual_cash(events: list[DividendEventDTO]) -> Optional[float]:
    """TTM（近 365 天）每股现金分红之和，最多取最近 2 笔。events 须按 ex_date 降序。

    累加窗口内已实施分红；上限 2 笔（年度 + 中期）避免分红日落在窗口两端时
    跨财年重复计入（如平安 6 月年度分红，12 个月窗口会同时含上/本年度）。
    旧实现只取最新一笔，导致茅台/平安等股息率偏低近一半。
    """
    if not events:
        return None
    cutoff = date.today() - timedelta(days=365)
    recent = [e for e in events if e.ex_date >= cutoff][:2]
    total = sum(e.per_share_cash for e in recent)
    return total or None


def build_metric_from_df(
    df: pd.DataFrame,
    code: str,
    currency: str = "CNY",
    max_events: int = 8,
    latest_price: Optional[float] = None,
    source: str = "akshare",
) -> Optional[DividendMetric]:
    """从分红 DataFrame 构建 DividendMetric（含 TTM，可选 yield）。无数据返回 None。"""
    work = _filter_rows(df, code)
    if work is None or work.empty:
        return None

    today = date.today()
    seen: set[tuple[str, float]] = set()
    events: list[DividendEventDTO] = []

    for _, row in work.iterrows():
        if not isinstance(row, pd.Series):
            continue
        ex = _to_date(_pick(row, _KW["ex_dividend_date"]))
        rd = _to_date(_pick(row, _KW["record_date"]))
        ad = _to_date(_pick(row, _KW["announce_date"]))
        ev_date = ex or rd or ad
        if not ev_date or ev_date > today:
            continue
        ps = _extract_per_share(row)
        if not ps or ps <= 0:
            continue
        key = (ev_date.isoformat(), round(ps, 6))
        if key in seen:
            continue
        seen.add(key)
        events.append(DividendEventDTO(
            ex_date=ev_date, per_share_cash=ps, record_date=rd,
            announce_date=ad, currency=currency, source=source,
        ))

    if not events:
        return None

    events.sort(key=lambda e: e.ex_date, reverse=True)
    latest_annual = _latest_annual_cash(events)
    yield_pct = round(latest_annual / latest_price * 100, 4) if (latest_annual and latest_price and latest_price > 0) else None
    return DividendMetric(
        code=code, currency=currency,
        ttm_cash_per_share=latest_annual or 0.0, ttm_yield_pct=yield_pct,
        events=events[:max_events], coverage="ttm_365d",
    )


def build_metric_from_history(
    df: pd.DataFrame,
    code: str,
    currency: str = "CNY",
    latest_price: Optional[float] = None,
    source: str = "akshare",
    max_events: int = 8,
) -> Optional[DividendMetric]:
    """专门处理 stock_history_dividend_detail：其「派息」列是【每 10 股派息额】，
    故每股 = 派息 / 10。这是 akshare 最稳定可靠的 A 股分红口径。"""
    if df is None or df.empty or "派息" not in df.columns:
        return None
    today = date.today()
    events: list[DividendEventDTO] = []
    for _, row in df.iterrows():
        if not isinstance(row, pd.Series):
            continue
        # 只取已「实施」的分红；预案/进度阶段尚未派发，且会与实施行重复计入
        prog = safe_str(row.get("进度"))
        if prog and prog != "实施":
            continue
        ex = _to_date(row.get("除权除息日")) or _to_date(row.get("股权登记日"))
        if not ex or ex > today:
            continue
        per10 = safe_float(row.get("派息"))
        if not per10 or per10 <= 0:
            continue
        # 每10股派现；若同时送股/转增，按除权后股本(10+送+转)摊到每股，
        # 与现价(除权后)口径一致，避免虚高（如比亚迪「送8转12派39.74」）
        song = safe_float(row.get("送股")) or 0.0
        zz = safe_float(row.get("转增")) or 0.0
        denom = 10.0 + song + zz
        events.append(DividendEventDTO(
            ex_date=ex, per_share_cash=round(per10 / denom, 6),
            currency=currency, source=source,
        ))
    if not events:
        return None
    events.sort(key=lambda e: e.ex_date, reverse=True)
    latest_annual = _latest_annual_cash(events)
    yp = round(latest_annual / latest_price * 100, 4) if (latest_annual and latest_price and latest_price > 0) else None
    return DividendMetric(
        code=code, currency=currency,
        ttm_cash_per_share=latest_annual or 0.0, ttm_yield_pct=yp,
        events=events[:max_events], coverage="ttm_365d",
    )


def build_metric_from_fhps(
    df: pd.DataFrame,
    code: str,
    currency: str = "CNY",
    latest_price: Optional[float] = None,
    source: str = "akshare",
    max_events: int = 8,
) -> Optional[DividendMetric]:
    """处理 stock_fhps_detail_em：有「报告期」(财年) 字段，按财年累加，根治跨财年。

    取「报告期为 12-31 的最近一个财年」的全部分红之和（含已实施 + 股东大会通过
    的预案），与行情软件（同花顺/东财）口径一致。
    列：报告期 / 现金分红-现金分红比例(每10股) / 现金分红-现金分红比例描述 /
    除权除息日 / 方案进度。
    """
    if df is None or df.empty or "报告期" not in df.columns:
        return None
    today = date.today()
    rows: list[tuple[date, float]] = []   # (报告期, 每股派息)
    events: list[DividendEventDTO] = []
    for _, row in df.iterrows():
        if not isinstance(row, pd.Series):
            continue
        prog = safe_str(row.get("方案进度"))
        if prog and prog not in ("实施分配", "实施", "股东大会决议通过"):
            continue   # 只取已实施 + 已通过预案
        rp = _to_date(row.get("报告期"))
        if not rp:
            continue
        ratio = safe_float(row.get("现金分红-现金分红比例"))   # 每 10 股派现
        desc = safe_str(row.get("现金分红-现金分红比例描述"))
        song = safe_float(row.get("送转股份-送股比例")) or 0.0
        zhuan = safe_float(row.get("送转股份-转股比例")) or 0.0   # 列名是「转股」不是「转增」
        denom = 10.0 + song + zhuan   # 送转股除权后股本，与现价(除权后)口径一致
        per = (ratio / denom) if (ratio and ratio > 0) else _parse_plan_to_per_share(desc)
        if not per or per <= 0:
            continue
        ex = _to_date(row.get("除权除息日"))
        if ex and ex > today:
            continue   # 未来除权跳过；预案 ex=NaT 用报告期当日期
        rows.append((rp, per))
        events.append(DividendEventDTO(
            ex_date=ex or rp, per_share_cash=round(per, 6),
            currency=currency, source=source,
        ))
    if not rows:
        return None
    # 最近一个完整财年：报告期含 12-31 的最大年份
    annual_years = sorted({rp.year for rp, _ in rows if rp.month == 12}, reverse=True)
    if annual_years:
        fy = annual_years[0]
        ttm = sum(per for rp, per in rows if rp.year == fy)
        cov = f"fiscal_{fy}"
    else:
        ttm = rows[0][1]
        cov = "latest_event"
    events.sort(key=lambda e: e.ex_date, reverse=True)
    yp = round(ttm / latest_price * 100, 4) if (latest_price and latest_price > 0) else None
    return DividendMetric(
        code=code, currency=currency,
        ttm_cash_per_share=round(ttm, 6), ttm_yield_pct=yp,
        events=events[:max_events], coverage=cov,
    )
