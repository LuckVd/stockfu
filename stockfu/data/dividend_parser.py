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
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    s = str(value).strip().replace(",", "").replace("%", "").replace("元", "")
    if not s or s in ("-", "--"):
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


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
    ttm_start = today - timedelta(days=365)
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
    ttm = round(sum(e.per_share_cash for e in events
                    if ttm_start <= e.ex_date <= today), 6)
    yield_pct = round(ttm / latest_price * 100, 4) if (latest_price and latest_price > 0) else None
    return DividendMetric(
        code=code, currency=currency,
        ttm_cash_per_share=ttm, ttm_yield_pct=yield_pct,
        events=events[:max_events], coverage="cash_dividend_pre_tax",
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
    ttm_start = today - timedelta(days=365)
    events: list[DividendEventDTO] = []
    for _, row in df.iterrows():
        if not isinstance(row, pd.Series):
            continue
        ex = (_to_date(row.get("除权除息日")) or _to_date(row.get("股权登记日"))
              or _to_date(row.get("公告日期")))
        if not ex or ex > today:
            continue
        per10 = safe_float(row.get("派息"))
        if not per10 or per10 <= 0:
            continue
        events.append(DividendEventDTO(
            ex_date=ex, per_share_cash=round(per10 / 10.0, 6),
            currency=currency, source=source,
        ))
    if not events:
        return None
    events.sort(key=lambda e: e.ex_date, reverse=True)
    ttm = round(sum(e.per_share_cash for e in events
                    if ttm_start <= e.ex_date <= today), 6)
    yp = round(ttm / latest_price * 100, 4) if (latest_price and latest_price > 0) else None
    return DividendMetric(
        code=code, currency=currency,
        ttm_cash_per_share=ttm, ttm_yield_pct=yp,
        events=events[:max_events], coverage="cash_dividend_pre_tax",
    )
