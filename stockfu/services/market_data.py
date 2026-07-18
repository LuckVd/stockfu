"""宏观 / 资金 / 情绪原始因子获取（akshare），供 composite 合成三层情绪指数。

每个函数多端点容错探测，失败返回 None / 空 dict，绝不抛异常 —— 拿不到的因子
在合成时自动跳过（缺因子不阻塞整体）。
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd


def _call(candidates):
    """逐个尝试 akshare 函数，返回首个非空 DataFrame。"""
    try:
        import akshare as ak
    except Exception:  # noqa: BLE001
        return None
    for name, kwargs in candidates:
        fn = getattr(ak, name, None)
        if fn is None:
            continue
        try:
            df = fn(**kwargs)
            if isinstance(df, pd.Series):
                df = df.to_frame().T
            if isinstance(df, pd.DataFrame) and not df.empty:
                return df
        except Exception:  # noqa: BLE001
            continue
    return None


def _f(v):
    try:
        s = str(v).replace(",", "").replace("%", "").replace("亿", "")
        return float(s) if s and s not in ("-", "--", "nan") else None
    except (TypeError, ValueError):
        return None


def _pick(row, *keys):
    for c in row.index:
        cs = str(c)
        if any(k in cs for k in keys):
            v = row.get(c)
            if v is not None and str(v).strip() not in ("", "-", "nan", "None"):
                return v
    return None


# ---------------- 全市场因子 ----------------

def market_breadth() -> dict | None:
    """全市场涨跌家数（广度）。"""
    df = _call([("stock_zh_a_spot_em", {})])
    if df is None:
        return None
    pct_col = next((c for c in df.columns if "涨跌幅" in str(c)), None)
    if pct_col is None:
        return None
    s = pd.to_numeric(df[pct_col], errors="coerce").dropna()
    up, down = int((s > 0).sum()), int((s < 0).sum())
    flat = int((s == 0).sum())
    total = up + down + flat
    return {"up": up, "down": down, "flat": flat, "total": total,
            "up_ratio": up / total if total else None,
            "down_ratio": down / total if total else None}


def limit_up_board() -> dict | None:
    """涨停连板：最高连板数 + 涨停家数（题材热度）。"""
    today = date.today()
    dates = [today - timedelta(days=i) for i in range(0, 6)]
    df = None
    for d in dates:  # 容错：今天没数据则往前找最近交易日
        df = _call([("stock_zt_pool_em", {"date": d.strftime("%Y%m%d")})])
        if df is not None:
            break
    if df is None:
        return None
    chain_col = next((c for c in df.columns if "连板" in str(c)), None)
    highest = None
    if chain_col:
        vals = pd.to_numeric(df[chain_col], errors="coerce").dropna()
        highest = int(vals.max()) if len(vals) else None
    return {"limit_up_count": len(df), "highest_chain": highest}


def limit_up_at(d) -> dict | None:
    """指定日期 d(date) 的涨停连板（供历史回补用）。
    非交易日/无涨停返回 count=0（正常空，不算失败）；接口异常返回 None。"""
    try:
        import akshare as ak
        df = ak.stock_zt_pool_em(date=d.strftime("%Y%m%d"))
    except Exception:  # noqa: BLE001
        return None
    if df is None or df.empty:
        return {"limit_up_count": 0, "highest_chain": None}  # 非交易日/无涨停
    chain_col = next((c for c in df.columns if "连板" in str(c)), None)
    highest = None
    if chain_col:
        vals = pd.to_numeric(df[chain_col], errors="coerce").dropna()
        highest = int(vals.max()) if len(vals) else None
    return {"limit_up_count": len(df), "highest_chain": highest}


def margin_total() -> dict | None:
    """两融余额总量（杠杆情绪）。"""
    df = _call([
        ("stock_margin_sse", {}),
        ("stock_margin_szse", {}),
    ])
    if df is None:
        return None
    row = df.iloc[0]  # stock_margin_sse 降序，首行最新
    return {"balance": _f(_pick(row, "融资融券余额", "余额", "两融余额")),
            "margin_balance": _f(_pick(row, "融资余额")),
            "source": "akshare"}


def bond_yield_10y() -> float | None:
    """十年期国债收益率（用于 ERP）。"""
    end = date.today().strftime("%Y%m%d")
    start = (date.today() - timedelta(days=30)).strftime("%Y%m%d")
    df = _call([("bond_china_yield", {"start_date": start, "end_date": end})])
    if df is None:
        return None
    ycol = next((c for c in df.columns if "10年" in str(c) or "10" in str(c)), None)
    if ycol is None:
        ycol = df.columns[-1]
    try:
        val = pd.to_numeric(df[ycol], errors="coerce").dropna().iloc[-1]
        return float(val)
    except Exception:  # noqa: BLE001
        return None


def erp(pe_300: float | None = None) -> dict | None:
    """股债利差 ERP = 沪深300盈利收益率(1/PE) − 十债收益率。

    pe_300 由调用方传入（来自 510300 实时或历史）。单位均为 %。
    """
    if not pe_300 or pe_300 <= 0:
        return None
    bond = bond_yield_10y()
    if bond is None:
        return None
    earnings_yield = 100.0 / pe_300        # 1/PE → %
    return {"erp": round(earnings_yield - bond, 4),
            "earnings_yield": round(earnings_yield, 4),
            "bond_10y": bond}


# ---------------- 个股因子 ----------------

def stock_margin(code: str) -> dict | None:
    """个股两融余额（杠杆资金态度）。stock_margin_detail_sse(date) 返回全市场，需筛 code；
    今日数据常未生成，往前找最近交易日。"""
    today = date.today()
    for i in range(8):
        d = (today - timedelta(days=i)).strftime("%Y%m%d")
        df = _call([
            ("stock_margin_detail_sse", {"date": d}),
            ("stock_margin_detail_szse", {"date": d}),
        ])
        if df is None or df.empty:
            continue
        code_col = next((c for c in df.columns if "代码" in str(c)), None)
        if code_col is None:
            continue
        row = df[df[code_col].astype(str).str.contains(code)]
        if len(row):
            r = row.iloc[0]
            return {"date": d, "balance": _f(_pick(r, "融资余额", "余额")),
                    "buy_amount": _f(_pick(r, "融资买入额", "买入额"))}
    return None
