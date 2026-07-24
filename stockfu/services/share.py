"""分享卡片：组装可公开的「市场 + 持仓」数据，供前端导出图片。

只含公开字段（现价 / 股息率 / 三层情绪指数 / 区间涨跌幅），
**不含** 持仓数量 / 成本 / 盈亏 / 市值 / 年红利 / 回本 等敏感数据。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from stockfu.services import factors as F


SHARE_INDEX_CODES = ("sh000001", "sz399006", "sh000688")


def export_readiness(target_date: date | None = None) -> dict:
    """校验分享卡片全部行情是否同属一个交易日。

    只检查卡片实际会展示的自选和三个市场指数；按品种路由行情表，避免 ETF
    遗留在 quote_snapshot 的旧行被误判为最新。未通过时拒绝出图，宁可不导出。
    """
    from sqlmodel import select

    from stockfu.db import session_scope
    from stockfu.models import Asset
    from stockfu.services.snapshot import latest_trade_date

    td = target_date or latest_trade_date()
    with session_scope() as s:
        watch_codes = list(s.exec(select(Asset.code).where(Asset.is_watch == True)).all())  # noqa: E712
        codes = list(dict.fromkeys(watch_codes + list(SHARE_INDEX_CODES)))
        stale: list[dict[str, str | None]] = []
        for code in codes:
            model = F.quote_model_for(code)
            row = s.exec(select(model.quote_date).where(
                model.asset_code == code
            ).order_by(model.quote_date.desc()).limit(1)).first()
            got = row if isinstance(row, date) else (row[0] if row else None)
            if got != td:
                stale.append({"code": code, "quote_date": got.isoformat() if got else None})
    return {"ok": not stale, "date": td.isoformat(), "stale": stale}


def perf(code: str, days: int, *, as_of: date) -> Optional[float]:
    """近 N 自然日涨跌幅% = 今收 / N 天前最近交易日收 - 1。

    以 as_of - N 自然日为起点，取该日及之后第一个交易日的收盘为基准，与 as_of
    当日收盘比较。这样周末、补发历史日报时，日期与数值可复现。
    修复：旧实现误用 quote_series(days+15) 的最早点，实际窗口是 ~N+30 自然日，
    把"近1周"算成了近 5 周（京东方A 曾因此显示 +52.8%，实际近1周应是 +12.4%）。
    """
    from sqlmodel import select

    from stockfu.db import session_scope

    start = as_of - timedelta(days=days)
    # 按代码路由行情表(个股 QuoteSnapshot / ETF EtfQuoteDaily / 指数 IndexQuoteDaily)
    model = F.quote_model_for(code)
    with session_scope() as s:
        rows = s.exec(select(model).where(
            model.asset_code == code, model.quote_date >= start,
            model.quote_date <= as_of,
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
    readiness = export_readiness(td)
    if not readiness["ok"]:
        bad = ", ".join(
            f"{x['code']}={x['quote_date'] or '缺失'}" for x in readiness["stale"][:8]
        )
        raise ValueError(f"分享数据未全部更新至 {td.isoformat()}（{bad}），已拒绝导出")
    sh_code = "sh000001"   # 大盘基准 = 上证指数（与 webUI 主看板一致）

    # 三个大盘指数行情 + 三层情绪（与 /indices/quotes 同源；pct_chg 缺失自动从 close 算）
    index_quotes = index_quotes_view()

    # 大盘情绪 = 上证指数的三层情绪（直接取用上证，不再用 compute_market 合成）
    sh = index_quotes.get("000001", {})
    market = {"fear": sh.get("fear"), "greed": sh.get("greed"), "heat": sh.get("heat"),
              "today_chg": sh.get("pct_chg"),
              "perf_1w": perf(sh_code, 7, as_of=td), "perf_1m": perf(sh_code, 30, as_of=td),
              "perf_1y": perf(sh_code, 365, as_of=td),
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
        "perf_1w": perf(p["code"], 7, as_of=td),
        "perf_1m": perf(p["code"], 30, as_of=td),
        "perf_1y": perf(p["code"], 365, as_of=td),
    } for p in wl]
    holdings.sort(key=lambda h: (h["day_chg"] if h["day_chg"] is not None else -999), reverse=True)

    return {"date": td.isoformat(), "market": market, "holdings": holdings}
