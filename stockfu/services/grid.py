"""股息率网格买卖计划。

思路（红利投资常用）：股息率 = 每股派息 / 价格。价格越低 → 股息率越高 → 越值得买；
价格越高 → 股息率越低 → 考虑卖出。给定一组股息率档位，反推对应价位，
对照当前股息率标注「买/持/卖」。
"""
from __future__ import annotations

from stockfu.data.manager import get_manager

# 默认股息率档位（%），从低到高：低档=高价=卖出区，高档=低价=买入区
DEFAULT_LEVELS = (2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 6.0)


def build_grid(code: str, levels: tuple[float, ...] = DEFAULT_LEVELS) -> dict | None:
    mgr = get_manager()
    q = mgr.get_quote(code)
    if not q:
        return None
    m = mgr.get_dividend_metric(code, latest_price=q.price)
    if not m or m.ttm_cash_per_share <= 0:
        return {"code": code, "name": q.name, "current_price": q.price,
                "current_yield": None, "ttm_cash": None, "rows": [],
                "note": "无分红数据，无法生成股息率网格"}

    cur_yield = m.ttm_yield_pct or 0.0
    rows = []
    for yld in sorted(levels):
        price = m.ttm_cash_per_share / (yld / 100.0)  # 价位 = 每股派息 / 目标股息率
        if yld >= cur_yield + 0.25:
            action = "买入"
        elif yld <= cur_yield - 0.75:
            action = "卖出"
        else:
            action = "持有"
        rows.append({"yield_pct": yld, "price": round(price, 3), "action": action})
    return {
        "code": code, "name": q.name, "currency": q.currency,
        "current_price": q.price, "current_yield": cur_yield,
        "ttm_cash": m.ttm_cash_per_share, "rows": rows,
    }
