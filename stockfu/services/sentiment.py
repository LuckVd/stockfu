"""板块情绪：各行业板块资金流排名 + 情绪温度。

数据源：akshare stock_sector_fund_flow_rank（东财，偶有反爬限流，失败返回空）。
情绪温度：净流入板块数 vs 净流出板块数，>0 偏热，<0 偏冷。
"""
from __future__ import annotations

from stockfu.data.manager import get_manager


def sector_board(top_n: int = 10) -> dict:
    sf = get_manager().get_sector_fund_flow(top_n)
    top = sf.get("top", [])
    bottom = sf.get("bottom", [])
    inflow = [x for x in top if (x.get("net") or 0) > 0]
    outflow = [x for x in bottom if (x.get("net") or 0) < 0]
    # 温度：净流入数 - 净流出数（归一到 -100..100 的粗略温度）
    diff = len(inflow) - len(outflow)
    temp = max(-100, min(100, diff * 100 / max(1, len(inflow) + len(outflow))))
    label = ("偏热" if temp > 30 else "偏冷" if temp < -30 else "中性")
    return {
        "inflow_top": [{"name": x["name"], "net_yi": round(x["net"] / 1e8, 2)} for x in inflow],
        "outflow_top": [{"name": x["name"], "net_yi": round(x["net"] / 1e8, 2)} for x in outflow],
        "temperature": round(temp, 1),
        "label": label,
        "source": sf.get("source", ""),
    }
