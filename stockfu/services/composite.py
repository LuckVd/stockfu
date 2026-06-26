"""三层情绪指数合成（市场 / 板块 / 个股）。

框架（CNN 式）：每个因子取自身历史分位(0-100) → 按指数方向等权平均。
- K线派生因子(volatility/momentum/amount)：从 quote_snapshot 算 rolling 序列 → 分位（立即可用）
- 外部因子(连板/两融/ERP/资金流/涨跌家数)：取当日值，分位从 factor_snapshot 历史算
  （首日无历史→跳过，随每日 --fetch 积累后生效；越跑越准）
方向：fear=下行(vol高/跌/资金流出/ERP高)，greed=上行(涨/放量/连板/两融升/资金流入)，heat=活跃(成交/连板数)。
"""
from __future__ import annotations

import json
import math
from datetime import date

from sqlmodel import select

from stockfu.db import session_scope
from stockfu.models import FactorSnapshot, IndexSnapshot
from stockfu.services import factors as F

BENCH = "510300"
MID = F.WINDOW_MID_DAYS  # 情绪/量价类 5 年窗口

# 板块/主题 → 代表 ETF（板块层用其 K 线 + 板块资金流）
SECTOR_MAP = {
    "沪深300": "510300", "中证500": "510500", "创业板": "159915",
    "科创50": "588000", "银行": "512800", "白酒": "512690",
    "半导体": "512480", "医药": "512010", "新能源车": "515030",
}


def _rolling_vol(closes, n=20):
    if len(closes) < n + 1:
        return []
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
    out = []
    for i in range(n, len(rets) + 1):
        w = rets[i - n:i]
        m = sum(w) / len(w)
        out.append(math.sqrt(sum((r - m) ** 2 for r in w) / len(w)) * math.sqrt(252))
    return out


def _rolling_chg(closes, n=5):
    return [closes[i] / closes[i - n] - 1 for i in range(n, len(closes))] if len(closes) > n else []


def _pct(series, value):
    if value is None:
        return None
    return F.percentile(series, value)[0]


def _ext_pct(level, scope, factor, today_val):
    """外部因子：从 factor_snapshot 读历史算当日值分位。"""
    if today_val is None:
        return None
    with session_scope() as s:
        rows = s.exec(select(FactorSnapshot).where(
            FactorSnapshot.level == level, FactorSnapshot.scope == scope,
            FactorSnapshot.factor == factor)).all()
    hist = [r.raw_value for r in rows if r.raw_value is not None]
    return F.percentile(hist, today_val)[0]  # 样本不足返回 None


def compute_for(code, level, scope, ext=None, val_pcts=None):
    """通用三层合成。

    ext={factor:(today_value, belong)}，belong∈fear/greed/heat（raw→历史分位）。
    val_pcts={name:分位} 已算好的分位，直接参与合成（如 baostock 的 PE/PB）。
    """
    ext = ext or {}
    closes = F.quote_series(code, "close", MID)
    amounts = F.quote_series(code, "amount", MID)
    comps, fp, gp, hp, ext_raws = {}, [], [], [], {}

    if len(closes) >= 30:
        vols, chgs = _rolling_vol(closes), _rolling_chg(closes)
        vol_pct = _pct(vols, vols[-1]) if vols else None
        chg_pct = _pct(chgs, chgs[-1]) if chgs else None
        amt_pct = _pct(amounts, amounts[-1]) if amounts else None
        comps.update(volatility_pct=vol_pct, momentum_pct=chg_pct, amount_pct=amt_pct)
        if vol_pct is not None:
            fp.append(vol_pct)
        if chg_pct is not None:
            fp.append(100 - chg_pct)   # 跌 → fear
            gp.append(chg_pct)         # 涨 → greed
        if amt_pct is not None:
            gp.append(amt_pct)
            hp.append(amt_pct)

    for name, (val, belong) in ext.items():
        ext_raws[name] = val
        p = _ext_pct(level, scope, name, val)
        comps[name] = {"raw": val, "pct": p}
        if p is not None:
            {"fear": fp, "greed": gp, "heat": hp}.get(belong, []).append(p)

    # 已算好的分位（如 baostock 的 PE/PB），直接参与合成
    for name, p in (val_pcts or {}).items():
        if p is not None:
            comps[name] = p
            fp.append(100 - p)   # 低分位(低估)=fear；高分位(高估)=greed
            gp.append(p)

    fear = round(sum(fp) / len(fp), 2) if fp else None
    greed = round(sum(gp) / len(gp), 2) if gp else None
    heat = round(sum(hp) / len(hp), 2) if hp else None
    return {"level": level, "scope": scope, "fear": fear, "greed": greed, "heat": heat,
            "components": comps, "ext_raws": ext_raws,
            "factor_counts": {"fear": len(fp), "greed": len(gp), "heat": len(hp)}}


def compute_market():
    from stockfu.data.manager import get_manager
    from stockfu.services import market_data as md

    q = get_manager().get_quote(BENCH)
    ext = {}
    try:
        lu = md.limit_up_board() or {}
        if lu.get("highest_chain") is not None:
            ext["limit_chain"] = (lu["highest_chain"], "greed")
        if lu.get("limit_up_count") is not None:
            ext["limit_count"] = (lu["limit_up_count"], "heat")
    except Exception:  # noqa: BLE001
        pass
    try:
        mt = md.margin_total() or {}
        if mt.get("balance"):
            ext["margin_balance"] = (mt["balance"], "greed")
    except Exception:  # noqa: BLE001
        pass
    try:
        if q and q.pe:
            er = md.erp(q.pe) or {}
            if er.get("erp") is not None:
                ext["erp"] = (er["erp"], "fear")
    except Exception:  # noqa: BLE001
        pass
    try:
        mb = md.market_breadth() or {}
        if mb.get("up_ratio") is not None:
            ext["breadth_up"] = (mb["up_ratio"], "greed")
        if mb.get("down_ratio") is not None:
            ext["breadth_down"] = (mb["down_ratio"], "fear")
    except Exception:  # noqa: BLE001
        pass
    return compute_for(BENCH, "market", "MARKET", ext)


def compute_stock(code):
    from stockfu.data.manager import get_manager
    from stockfu.services import market_data as md

    ext = {}
    try:
        ff = get_manager().get_stock_fund_flow(code) or {}
        mi = ff.get("main_net_inflow")
        if mi is not None:
            ext["fund_flow_main"] = (mi, "greed" if mi > 0 else "fear")
    except Exception:  # noqa: BLE001
        pass
    try:
        sm = md.stock_margin(code) or {}
        if sm.get("buy_amount"):
            ext["margin_buy"] = (sm["buy_amount"], "greed")
    except Exception:  # noqa: BLE001
        pass
    # 估值因子：PE/PB 历史分位（baostock，免费替代 tushare）
    val_pcts = {}
    try:
        pe_pct, pb_pct = get_manager().baostock.get_pe_pb_percentile(code)
        if pe_pct is not None:
            val_pcts["pe_pct"] = pe_pct
        if pb_pct is not None:
            val_pcts["pb_pct"] = pb_pct
    except Exception:  # noqa: BLE001
        pass
    return compute_for(code, "stock", code, ext, val_pcts=val_pcts)


def compute_sector(etf_code, name):
    """板块层：用板块代表 ETF 的 K 线 + 板块资金流。"""
    from stockfu.data.manager import get_manager

    ext = {}
    try:
        sf = get_manager().get_sector_fund_flow() or {}
        for x in sf.get("top", []) + sf.get("bottom", []):
            if name in str(x.get("name", "")):
                ext["sector_flow"] = (x["net"], "greed" if x["net"] > 0 else "fear")
                break
    except Exception:  # noqa: BLE001
        pass
    return compute_for(etf_code, "sector", name, ext)


def save(result) -> None:
    """存 index_snapshot(fear/greed/heat) + factor_snapshot(外部因子 raw)。"""
    level, scope = result["level"], result["scope"]
    today = date.today()
    comps, ext_raws = result.get("components", {}), result.get("ext_raws", {})
    with session_scope() as s:
        for key in ("fear", "greed", "heat"):
            val = result.get(key)
            if val is None:
                continue
            ex = s.exec(select(IndexSnapshot).where(
                IndexSnapshot.index_key == key, IndexSnapshot.level == level,
                IndexSnapshot.scope == scope, IndexSnapshot.snap_date == today)).first()
            snap = ex or IndexSnapshot(index_key=key, level=level, scope=scope, snap_date=today)
            snap.value = val
            snap.components = json.dumps(comps, ensure_ascii=False, default=str)[:4000]
            if snap.id is None:
                s.add(snap)
        for factor, raw in ext_raws.items():
            if raw is None:
                continue
            ex = s.exec(select(FactorSnapshot).where(
                FactorSnapshot.level == level, FactorSnapshot.scope == scope,
                FactorSnapshot.factor == factor, FactorSnapshot.snap_date == today)).first()
            fs = ex or FactorSnapshot(level=level, scope=scope, factor=factor, snap_date=today)
            fs.raw_value = float(raw)
            if fs.id is None:
                s.add(fs)
        s.commit()


def compute_all(stocks=None) -> dict:
    """算 市场 + 所有个股 + 所有板块 三层，逐个落库。返回 {key: result} 摘要。"""
    out: dict[str, dict] = {}
    out["market"] = compute_market()
    save(out["market"])
    for code in (stocks or []):
        try:
            r = compute_stock(code)
            out[f"stock:{code}"] = r
            save(r)
        except Exception as exc:  # noqa: BLE001
            out[f"stock:{code}"] = {"error": str(exc)}
    for name, etf in SECTOR_MAP.items():
        try:
            r = compute_sector(etf, name)
            out[f"sector:{name}"] = r
            save(r)
        except Exception as exc:  # noqa: BLE001
            out[f"sector:{name}"] = {"error": str(exc)}
    return out
