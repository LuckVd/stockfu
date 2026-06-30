"""板块情绪：各行业板块资金流排名 + 情绪温度。

数据源：同花顺板块资金流（get_sector_flow_today，绕开东财限流，列全且稳定）。
情绪温度：净流入板块数 vs 净流出板块数，>0 偏热，<0 偏冷。
"""
from __future__ import annotations

from stockfu.data.manager import get_manager


def sector_board(top_n: int = 10) -> dict:
    """板块情绪：行业资金流排名 + 温度。

    数据走同花顺 get_sector_flow_today（net_inflow 单位：亿元）。
    返回前端期望的 top/bottom（每项 {name, net(元)}，前端按亿展示），
    同时保留旧字段 inflow_top/outflow_top + temperature/label 供兼容。
    """
    rows = get_manager().get_sector_flow_today() or []
    rows = sorted(rows, key=lambda x: x.get("net_inflow") or 0, reverse=True)

    def _item(x):
        return {"name": x.get("name", ""), "net": round((x.get("net_inflow") or 0) * 1e8)}

    top = [_item(x) for x in rows[:top_n] if (x.get("net_inflow") or 0) > 0]
    neg = [x for x in rows if (x.get("net_inflow") or 0) < 0]
    bottom = [_item(x) for x in neg[-top_n:]]           # 净流出最多的 top_n 个

    inflow = [x for x in top if x["net"] > 0]
    outflow = [x for x in bottom if x["net"] < 0]
    # 温度：净流入数 - 净流出数（归一到 -100..100 的粗略温度）
    diff = len(inflow) - len(outflow)
    temp = max(-100, min(100, diff * 100 / max(1, len(inflow) + len(outflow))))
    label = ("偏热" if temp > 30 else "偏冷" if temp < -30 else "中性")
    return {
        "top": top,
        "bottom": bottom,
        "inflow_top": [{"name": x["name"], "net_yi": round(x["net"] / 1e8, 2)} for x in inflow],
        "outflow_top": [{"name": x["name"], "net_yi": round(x["net"] / 1e8, 2)} for x in outflow],
        "temperature": round(temp, 1),
        "label": label,
        "source": "同花顺",
    }
