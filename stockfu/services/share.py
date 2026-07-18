"""分享卡片：组装可公开的「市场 + 持仓」数据，供前端导出图片。

只含公开字段（现价 / 股息率 / 三层情绪指数 / 区间涨跌幅），
**不含** 持仓数量 / 成本 / 盈亏 / 市值 / 年红利 / 回本 等敏感数据。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from stockfu.services import factors as F


def perf(code: str, days: int) -> Optional[float]:
    """近 N 自然日涨跌幅% = 今收 / N 天前最近交易日收 - 1。

    以 today - N 自然日为起点，取该日及之后第一个交易日的收盘为基准，与最新收盘比较。
    修复：旧实现误用 quote_series(days+15) 的最早点，实际窗口是 ~N+30 自然日，
    把"近1周"算成了近 5 周（京东方A 曾因此显示 +52.8%，实际近1周应是 +12.4%）。
    """
    from sqlmodel import select

    from stockfu.db import session_scope
    from stockfu.models import IndexQuoteDaily, QuoteSnapshot

    start = date.today() - timedelta(days=days)
    # 指数(sh/sz 前缀)行情在 IndexQuoteDaily；个股/ETF(纯数字代码)在 QuoteSnapshot
    model = IndexQuoteDaily if code.startswith(("sh", "sz")) else QuoteSnapshot
    with session_scope() as s:
        rows = s.exec(select(model).where(
            model.asset_code == code, model.quote_date >= start,
        ).order_by(model.quote_date)).all()
    closes = [r.close for r in rows if r.close is not None]
    if len(closes) < 2 or not closes[0]:
        return None
    return round((closes[-1] / closes[0] - 1) * 100, 2)


def day_chg(code: str, cur: Optional[float] = None) -> Optional[float]:
    """当日涨跌幅%。优先用已落盘的 pct_chg；为空（历史 K线没存 pct_chg）则从 close 序列算。"""
    if cur is not None:
        return cur
    cs = F.quote_series(code, "close", 5)
    if len(cs) >= 2 and cs[-1]:
        return round((cs[-1] / cs[-2] - 1) * 100, 2)
    return None


def build_card() -> dict:
    """组装分享卡片数据（仅公开字段，脱敏）。"""
    from stockfu.services import portfolio as P
    from stockfu.services.snapshot import index_quotes_view, latest_trade_date

    td = latest_trade_date() or date.today()   # 卡片日期 = 最近交易日（不是今天）
    sh_code = "sh000001"   # 大盘基准 = 上证指数（与 webUI 主看板一致）

    # 三个大盘指数行情 + 三层情绪（与 /indices/quotes 同源；pct_chg 缺失自动从 close 算）
    index_quotes = index_quotes_view()

    # 大盘情绪 = 上证指数的三层情绪（直接取用上证，不再用 compute_market 合成）
    sh = index_quotes.get("000001", {})
    market = {"fear": sh.get("fear"), "greed": sh.get("greed"), "heat": sh.get("heat"),
              "today_chg": sh.get("pct_chg"),
              "perf_1w": perf(sh_code, 7), "perf_1m": perf(sh_code, 30), "perf_1y": perf(sh_code, 365),
              "index_quotes": index_quotes}

    # 自选/追踪股：只取公开字段 + perf（不含 shares/cost/profit 等敏感数据）
    wl = P.get_watchlist_view()
    holdings = [{
        "code": p["code"],
        "name": p["name"] or p["code"],
        "currency": p["currency"],
        "price": p["price"],
        "ttm_yield_pct": p["ttm_yield_pct"],
        "fear": p["fear"],
        "greed": p["greed"],
        "heat": p["heat"],
        "day_chg": day_chg(p["code"], p["day_chg"]),
        "perf_1w": perf(p["code"], 7),
        "perf_1m": perf(p["code"], 30),
        "perf_1y": perf(p["code"], 365),
    } for p in wl]
    holdings.sort(key=lambda h: (h["day_chg"] if h["day_chg"] is not None else -999), reverse=True)

    return {"date": td.isoformat(), "market": market, "holdings": holdings}
