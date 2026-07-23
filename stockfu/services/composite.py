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
from stockfu.models import FactorSnapshot, IndexSnapshot, SectorFlowSnapshot, SectorSnapshot
from stockfu.services import factors as F

BENCH = "512100"  # 大盘基准：中证1000ETF（中小盘代表，比沪深300 更能反映全市场）
MID = F.WINDOW_MID_DAYS  # 情绪/量价类 5 年窗口

# 板块/主题 → 代表 ETF（板块层用其 K 线 + 板块资金流）
SECTOR_MAP = {
    "沪深300": "510300", "中证500": "510500", "创业板": "159915",
    "科创50": "588000", "银行": "512800", "白酒": "512690",
    "半导体": "512480", "医药": "512010", "新能源车": "515030",
}

# SECTOR_MAP 键 → 同花顺行业精确名（stock_board_industry_index_ths / stock_fund_flow_industry 的 symbol）。
# None = 宽基指数/无对应行业 → 板块K线与资金流跳过，compute_sector 自动降级为纯 ETF 分位。
# 医药/新能源车 同花顺无单一对应行业，取代表性细分行业代理（可按需调整）。
SECTOR_THS_NAME = {
    "沪深300": None, "中证500": None, "创业板": None, "科创50": None,
    "银行": "银行", "白酒": "白酒", "半导体": "半导体",
    "医药": "医疗服务",
    "新能源车": "汽车整车",
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


def _ext_pct(level, scope, factor, today_val, as_of=None):
    """外部因子：从 factor_snapshot 读历史(<=as_of)算当日值分位。"""
    if today_val is None:
        return None
    with session_scope() as s:
        stmt = select(FactorSnapshot).where(
            FactorSnapshot.level == level, FactorSnapshot.scope == scope,
            FactorSnapshot.factor == factor)
        if as_of is not None:
            stmt = stmt.where(FactorSnapshot.snap_date <= as_of)
        rows = s.exec(stmt).all()
    hist = [r.raw_value for r in rows if r.raw_value is not None]
    return F.percentile(hist, today_val)[0]  # 样本不足返回 None


def _sector_series(name: str, model, field: str, days: int, as_of=None) -> list[float]:
    """读板块 raw 表(sector_snapshot/sector_flow_snapshot) 近 days 日(<=as_of)某字段序列。"""
    from datetime import date as _d, timedelta as _td
    ref = as_of or _d.today()
    start = ref - _td(days=days + 15)
    with session_scope() as s:
        rows = s.exec(select(model).where(
            model.sector_name == name, model.snap_date >= start,
            model.snap_date <= ref,
        ).order_by(model.snap_date)).all()
    return [getattr(r, field) for r in rows if getattr(r, field) is not None]


def _sector_today(name: str, model, field: str, as_of=None):
    """读板块 raw 表最新一行(<=as_of)的 field 值（无则 None）。"""
    with session_scope() as s:
        stmt = select(model).where(model.sector_name == name)
        if as_of is not None:
            stmt = stmt.where(model.snap_date <= as_of)
        row = s.exec(stmt.order_by(model.snap_date.desc())).first()
    return getattr(row, field, None) if row else None


def _sector_field_pct(name: str, model, field: str, today=None, as_of=None) -> float | None:
    """板块 raw 表某字段当日值的历史分位（样本<10 返回 None）。today 默认取序列末值。"""
    series = _sector_series(name, model, field, MID, as_of=as_of)
    if not series:
        return None
    val = today if today is not None else series[-1]
    return F.percentile(series, val)[0]


def _call_timeout(fn, timeout: float = 20.0):
    """带超时跑 fn（外部网络因子：baostock/东财），超时或异常返回 None。

    这些调用在后台 daemon 线程里可能卡住（baostock 非线程安全、东财反爬），
    若不设超时会阻塞整个指数计算——而 fear/greed 主要来自本地 K 线分位，
    不应被外部因子拖累。卡住即跳过该因子，K 线分位照常算。
    """
    import threading
    box: dict = {}

    def _run():
        try:
            box["r"] = fn()
        except Exception:  # noqa: BLE001
            box["e"] = True

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    return box.get("r")


def compute_for(code, level, scope, ext=None, val_pcts=None, ext_pcts=None, as_of=None):
    """通用三层合成。

    as_of: 信号日；K 线序列与外部因子历史均限制在 <= as_of（防未来函数）。
    ext={factor:(today_value, belong)}，belong∈fear/greed/heat（raw→历史分位，历史攒在 factor_snapshot）。
    val_pcts={name:分位} 已算好的分位，同时进 fear(100-p)+greed(p)（估值语义，如 PE/PB）。
    ext_pcts={name:(分位, belong)} 已算好的分位按指定 belong 进单一桶（成交额→heat 等非估值因子）。
    """
    ext = ext or {}
    closes = F.quote_series(code, "close", MID, as_of=as_of)
    amounts = F.quote_series(code, "amount", MID, as_of=as_of)
    volumes = F.quote_series(code, "volume", MID, as_of=as_of)
    comps, fp, gp, hp, ext_raws = {}, [], [], [], {}

    if len(closes) >= 30:
        vols, chgs = _rolling_vol(closes), _rolling_chg(closes)
        vol_pct = _pct(vols, vols[-1]) if vols else None
        chg_pct = _pct(chgs, chgs[-1]) if chgs else None
        # 成交活跃度：优先成交额(amount)，样本不足则回退成交量(volume)
        amt_series = amounts if len(amounts) >= 10 else volumes
        amt_pct = _pct(amt_series, amt_series[-1]) if amt_series else None
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
        p = _ext_pct(level, scope, name, val, as_of=as_of)
        comps[name] = {"raw": val, "pct": p}
        if p is not None:
            {"fear": fp, "greed": gp, "heat": hp}.get(belong, []).append(p)

    # 已算好的分位（如 baostock 的 PE/PB），直接参与合成
    for name, p in (val_pcts or {}).items():
        if p is not None:
            comps[name] = p
            fp.append(100 - p)   # 低分位(低估)=fear；高分位(高估)=greed
            gp.append(p)

    # 已算好分位 + 指定 belong（板块成交额→heat 等，区别于 val_pcts 的估值语义）
    for nm, (p, belong) in (ext_pcts or {}).items():
        if p is not None:
            comps[nm] = p
            {"fear": fp, "greed": gp, "heat": hp}.get(belong, []).append(p)

    fear = round(sum(fp) / len(fp), 2) if fp else None
    greed = round(sum(gp) / len(gp), 2) if gp else None
    heat = round(sum(hp) / len(hp), 2) if hp else None
    today_chg = round((closes[-1] / closes[-2] - 1) * 100, 2) if len(closes) >= 2 else None
    return {"level": level, "scope": scope, "fear": fear, "greed": greed, "heat": heat,
            "today_chg": today_chg,
            "components": comps, "ext_raws": ext_raws,
            "factor_counts": {"fear": len(fp), "greed": len(gp), "heat": len(hp)}}


def _ensure_bench_kline(min_bars: int = 60) -> None:
    """大盘基准(510300)K线不足则补——大盘指数依赖它，否则恒为空。"""
    from stockfu.db import session_scope
    from stockfu.models import QuoteSnapshot
    with session_scope() as s:
        n = len(s.exec(select(QuoteSnapshot).where(
            QuoteSnapshot.asset_code == BENCH)).all())
    if n < min_bars:
        from stockfu.scheduler.jobs import backfill_kline
        backfill_kline(BENCH, 1825)


def compute_market(as_of=None):
    from stockfu.data.manager import get_manager
    from stockfu.services import market_data as md

    _ensure_bench_kline()
    from stockfu.services.snapshot import latest_snapshot
    snap = latest_snapshot(BENCH)
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
        if snap and snap.pe:
            er = md.erp(snap.pe) or {}
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
    return compute_for(BENCH, "market", "MARKET", ext, as_of=as_of)


def compute_stock(code, as_of=None):
    from stockfu.data.manager import get_manager
    from stockfu.services import market_data as md

    ext = {}
    ff = _call_timeout(lambda: get_manager().get_stock_fund_flow(code)) or {}
    mi = ff.get("main_net_inflow")
    if mi is not None:
        ext["fund_flow_main"] = (mi, "greed" if mi > 0 else "fear")
    sm = _call_timeout(lambda: md.stock_margin(code)) or {}
    if sm.get("buy_amount"):
        ext["margin_buy"] = (sm["buy_amount"], "greed")
    # 估值因子：PE/PB 历史分位（baostock，免费替代 tushare）
    # baostock 进程级连接偶发掉线（_logged_in 仍 True、不重连 → query 静默返回空），
    # 拿不到有效分位时 force_relogin 后重试 1-2 次，根治偶发空返回。
    from stockfu.data.baostock_source import BaostockSource
    val_pcts = {}
    pe_pb = None
    for _ in range(3):  # 首试 + 2 次重试
        pe_pb = _call_timeout(lambda: get_manager().baostock.get_pe_pb_percentile(code))
        if isinstance(pe_pb, tuple) and (pe_pb[0] is not None or pe_pb[1] is not None):
            break
        BaostockSource.force_relogin()  # 掉线 → 强制重连再试
    if isinstance(pe_pb, tuple):
        pe_pct, pb_pct = pe_pb
        if pe_pct is not None:
            val_pcts["pe_pct"] = pe_pct
        if pb_pct is not None:
            val_pcts["pb_pct"] = pb_pct
    return compute_for(code, "stock", code, ext, val_pcts=val_pcts, as_of=as_of)


def compute_sector(etf_code, name, as_of=None):
    """板块层：代表 ETF 的 K 线分位 + 板块自身成交额分位(heat) + 资金流(greed/fear)。

    板块自身数据从 sector_snapshot / sector_flow_snapshot 读（表里存业务名 name）；
    仅 SECTOR_THS_NAME 有映射的板块才有；无映射(宽基)或无数据 → 降级为纯 ETF 分位。
    """
    from stockfu.data.manager import get_manager

    ext, ext_pcts = {}, {}
    if SECTOR_THS_NAME.get(name):              # 有同花顺行业映射才查板块自身数据
        # 板块成交额历史分位 → heat（sector_snapshot 有 ~4 年历史，立刻有分位）
        try:
            amt = _sector_today(name, SectorSnapshot, "amount", as_of=as_of)
            if amt is not None:
                p = _sector_field_pct(name, SectorSnapshot, "amount", today=amt, as_of=as_of)
                if p is not None:
                    ext_pcts["sector_amount"] = (p, "heat")
        except Exception:  # noqa: BLE001
            pass
        # 板块净流入：有历史分位用分位(greed)，否则当日方向(ext→落 factor_snapshot 攒)
        try:
            net = _sector_today(name, SectorFlowSnapshot, "net_inflow", as_of=as_of)
            if net is not None:
                p = _sector_field_pct(name, SectorFlowSnapshot, "net_inflow", today=net, as_of=as_of)
                if p is not None:
                    ext_pcts["sector_flow"] = (p, "greed")    # 高分位=持续流入
                else:
                    ext["sector_flow"] = (net, "greed" if net > 0 else "fear")
        except Exception:  # noqa: BLE001
            pass
    # 兜底：净流入未落库时沿用东财实时排名（push2 限流返回空→自动跳过）
    if "sector_flow" not in ext and "sector_flow" not in ext_pcts:
        try:
            sf = get_manager().get_sector_fund_flow() or {}
            for x in sf.get("top", []) + sf.get("bottom", []):
                if SECTOR_THS_NAME.get(name) in str(x.get("name", "")):
                    ext["sector_flow"] = (x["net"], "greed" if x["net"] > 0 else "fear")
                    break
        except Exception:  # noqa: BLE001
            pass
    return compute_for(etf_code, "sector", name, ext, ext_pcts=ext_pcts, as_of=as_of)


def save(result, snap_date=None) -> None:
    """存 index_snapshot(fear/greed/heat) + factor_snapshot(外部因子 raw)。

    snap_date: 盖章日(目标交易日)；None→今天(兼容零散调用，ingest 路径必传)。
    """
    level, scope = result["level"], result["scope"]
    d = snap_date or date.today()
    comps, ext_raws = result.get("components", {}), result.get("ext_raws", {})
    with session_scope() as s:
        for key in ("fear", "greed", "heat"):
            val = result.get(key)
            if val is None:
                continue
            ex = s.exec(select(IndexSnapshot).where(
                IndexSnapshot.index_key == key, IndexSnapshot.level == level,
                IndexSnapshot.scope == scope, IndexSnapshot.snap_date == d)).first()
            snap = ex or IndexSnapshot(index_key=key, level=level, scope=scope, snap_date=d)
            snap.value = val
            snap.components = json.dumps(comps, ensure_ascii=False, default=str)[:4000]
            if snap.id is None:
                s.add(snap)
        for factor, raw in ext_raws.items():
            if raw is None:
                continue
            ex = s.exec(select(FactorSnapshot).where(
                FactorSnapshot.level == level, FactorSnapshot.scope == scope,
                FactorSnapshot.factor == factor, FactorSnapshot.snap_date == d)).first()
            fs = ex or FactorSnapshot(level=level, scope=scope, factor=factor, snap_date=d)
            fs.raw_value = float(raw)
            if fs.id is None:
                s.add(fs)
        s.commit()


def compute_all(stocks=None, as_of=None) -> dict:
    """算 市场 + 所有个股 + 所有板块 三层，逐个落库。返回 {key: result} 摘要。

    as_of: 信号日(目标交易日)，传给各 compute_* 与 save 盖章。
    """
    out: dict[str, dict] = {}
    out["market"] = compute_market(as_of=as_of)
    save(out["market"], snap_date=as_of)
    for code in (stocks or []):
        try:
            r = compute_stock(code, as_of=as_of)
            out[f"stock:{code}"] = r
            save(r, snap_date=as_of)
        except Exception as exc:  # noqa: BLE001
            out[f"stock:{code}"] = {"error": str(exc)}
    for name, etf in SECTOR_MAP.items():
        try:
            r = compute_sector(etf, name, as_of=as_of)
            out[f"sector:{name}"] = r
            save(r, snap_date=as_of)
        except Exception as exc:  # noqa: BLE001
            out[f"sector:{name}"] = {"error": str(exc)}
    return out
