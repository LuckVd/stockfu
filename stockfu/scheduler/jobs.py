"""每日定时任务：抓行情/分红/ETF份额 → 落库(天级快照) → 算指数。

触发方式：
  python main.py --fetch      # 立即跑一次 run_scheduled_fetch
  python main.py --backfill   # 回填关键标的 90 日历史（首次必跑，指数才能算）
  python main.py --schedule   # APScheduler 按 daily_cron 长驻运行
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlmodel import select

from stockfu.db import init_db, session_scope
from stockfu.models import (Asset, EtfQuoteDaily, FundFlowSnapshot, IndexQuoteDaily, QuoteSnapshot)

# 指数基准 + 资金流追踪标的：宽基 & 热门行业 ETF
INDEX_ETFS = [
    "510300",  # 沪深300
    "510500",  # 中证500
    "159915",  # 创业板ETF
    "512100",  # 中证1000
    "588000",  # 科创50
    "512480",  # 半导体
    "512690",  # 白酒
    "512010",  # 医药
    "515030",  # 新能源车
    "512800",  # 银行
]


def _upsert_quote(code: str, timeout: float = 35) -> bool:
    """拉最近交易日行情落 quote_snapshot。

    quote_date 优先取自 K 线最后一条 bar.date（真实交易日，自动跳周末/节假日）；
    K 线不支持的代码（如指数 sh000001）→ fallback 实时报价 + 交易日历推算的最近交易日。
    不再用抓取日：实时报价不返回交易日，周末/节假日抓的会被错标。
    pe/pb/market_cap/name 始终从实时 get_quote 补。最近交易日已落盘则跳过。
    timeout: 单个标的超时秒数（港美股 yfinance 代理挂死时自动跳过）。
    """
    import threading
    box: dict = {}

    def _run():
        from stockfu.data.manager import get_manager
        from stockfu.services.snapshot import latest_trade_date
        try:
            m = get_manager()
            bars = m.get_kline(code, 10)
            q = m.get_quote(code)
            box["bars"], box["q"] = bars, q
        except Exception:
            box["e"] = True

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    if "e" in box or ("bars" not in box and "q" not in box):
        return False
    bars, q = box.get("bars"), box.get("q")
    if bars:
        bars = sorted(bars, key=lambda b: b.date)
        bar = bars[-1]
        tday = bar.date
        o, h, l, c, vol, amt = bar.open, bar.high, bar.low, bar.close, bar.volume, bar.amount
        pct = round((bar.close / bars[-2].close - 1) * 100, 2) if len(bars) >= 2 and bars[-2].close else None
    elif q:
        tday = latest_trade_date()
        o, h, l, c, vol, amt = q.open, q.high, q.low, q.price, q.volume, q.amount
        pct = q.pct_chg
    else:
        return False
    # 日状态从 K 线 bar 取(baostock 有;其它源多为 None)
    bar_ts = bar_st = None
    if bars:
        _b = bars[-1]
        bar_ts = getattr(_b, "trade_status", None)
        bar_st = getattr(_b, "is_st", None)

    with session_scope() as s:
        existing = s.exec(select(QuoteSnapshot).where(
            QuoteSnapshot.asset_code == code, QuoteSnapshot.quote_date == tday)).first()
        if existing and existing.close:
            # OHLCV 已有:仍补空状态列(防「旧行 NULL 永久静默」)
            ch = False
            if existing.trade_status is None and bar_ts is not None:
                existing.trade_status = int(bar_ts)
                ch = True
            if existing.is_st is None and bar_st is not None:
                existing.is_st = int(bar_st)
                ch = True
            if ch:
                s.commit()
            return True
        snap = existing or QuoteSnapshot(asset_code=code, quote_date=tday)
        # 有完整 bar 时走全量写入(含状态/估值);否则用实时 quote 兜底
        if bars:
            prev = bars[-2].close if len(bars) >= 2 else None
            _apply_bar_full(snap, bars[-1], prev)
        else:
            snap.open, snap.high, snap.low, snap.close = o, h, l, c
            snap.pct_chg, snap.volume, snap.amount = pct, vol, amt
        if q:
            if q.pe is not None:
                snap.pe = q.pe
            if q.pb is not None:
                snap.pb = q.pb
            if q.market_cap is not None:
                snap.market_cap = q.market_cap
        if snap.id is None:
            s.add(snap)
        if q and q.name:                          # 回填 Asset.name
            a = s.get(Asset, code)
            if a and not a.name:
                a.name = q.name
        s.commit()
    return True


def clean_quote_snapshots() -> dict:
    """删除 quote_snapshot 里 quote_date 不在 A 股交易日历的记录（周末/节假日错标）。

    这些是「非交易日抓取、被错标为抓取日」产生的重复数据，删除安全（真实交易日记录保留）。
    """
    from stockfu.services.snapshot import _trade_calendar
    cal = _trade_calendar()
    if not cal:
        return {"deleted": 0, "note": "交易日历不可用，跳过"}
    deleted = 0
    with session_scope() as s:
        for r in s.exec(select(QuoteSnapshot)).all():
            if r.quote_date not in cal:
                s.delete(r)
                deleted += 1
        s.commit()
    return {"deleted": deleted}


def _upsert_fundflow(code: str) -> bool:
    from stockfu.data.manager import get_manager
    ff = get_manager().get_etf_fund_flow(code)
    if not ff:
        return False
    today = date.today()
    with session_scope() as s:
        snap = s.exec(select(FundFlowSnapshot).where(
            FundFlowSnapshot.etf_code == code, FundFlowSnapshot.snap_date == today)).first()
        snap = snap or FundFlowSnapshot(etf_code=code, snap_date=today)
        snap.nav, snap.shares_outstanding = ff.get("nav"), ff.get("shares")
        snap.net_inflow = ff.get("amount")
        if snap.id is None:
            s.add(snap)
        s.commit()
    return True


def _bar_pct(b, prev_close: float | None) -> float | None:
    """涨跌幅%:优先 bar.pct_chg,否则用 prev_close 推。"""
    if getattr(b, "pct_chg", None) is not None:
        return float(b.pct_chg)
    if prev_close and prev_close > 0 and b.close:
        return round((b.close / prev_close - 1) * 100, 2)
    return None


def _apply_bar_full(snap: QuoteSnapshot, b, prev_close: float | None = None) -> None:
    """把一根 K 线完整写入 snapshot(OHLCV + 状态 + 估值 + 换手)。

    用于「最新交易日全量补全」:有值就覆盖,None 不抹掉已有非空字段。
    """
    snap.open = b.open
    snap.high = b.high
    snap.low = b.low
    snap.close = b.close
    if b.volume is not None:
        snap.volume = b.volume
    if b.amount is not None:
        snap.amount = b.amount
    pct = _bar_pct(b, prev_close)
    if pct is not None:
        snap.pct_chg = pct
    if getattr(b, "trade_status", None) is not None:
        snap.trade_status = int(b.trade_status)
    if getattr(b, "is_st", None) is not None:
        snap.is_st = int(b.is_st)
    if getattr(b, "pe", None) is not None:
        snap.pe = float(b.pe)
    if getattr(b, "pb", None) is not None:
        snap.pb = float(b.pb)
    if getattr(b, "turnover", None) is not None:
        snap.turnover = float(b.turnover)


def _patch_status_only(snap: QuoteSnapshot, b) -> bool:
    """仅补空的 is_st/trade_status。返回是否有改动。"""
    ch = False
    if snap.trade_status is None and getattr(b, "trade_status", None) is not None:
        snap.trade_status = int(b.trade_status)
        ch = True
    if snap.is_st is None and getattr(b, "is_st", None) is not None:
        snap.is_st = int(b.is_st)
        ch = True
    return ch


def backfill_kline(code: str, days: int = 90) -> int:
    """拉历史日K灌入 quote_snapshot（首次/补数据）。返回新增+补丁条数。

    - 缺失日:全字段插入
    - 已有历史日:仅补空状态列
    - **最新一根 bar**:始终全量覆盖(OHLCV+状态+估值),保证最新交易日数据完整
    """
    from stockfu.data.manager import get_manager
    bars = sorted(get_manager().get_kline(code, days), key=lambda b: b.date)
    if not bars:
        return 0
    n = 0
    patched = 0
    latest_d = bars[-1].date
    with session_scope() as s:
        existing = {
            q.quote_date: q for q in s.exec(
                select(QuoteSnapshot).where(QuoteSnapshot.asset_code == code)).all()
        }
        prev_close = None
        for b in bars:
            is_latest = (b.date == latest_d)
            if b.date not in existing:
                snap = QuoteSnapshot(asset_code=code, quote_date=b.date)
                _apply_bar_full(snap, b, prev_close)
                s.add(snap)
                n += 1
            elif is_latest:
                # 最新交易日:全量刷新
                _apply_bar_full(existing[b.date], b, prev_close)
                n += 1
            else:
                if _patch_status_only(existing[b.date], b):
                    patched += 1
            prev_close = b.close if b.close else prev_close
        s.commit()
    return n + patched


def backfill_quote_status(codes: list[str] | None = None, days: int = 2000) -> dict:
    """补全 quote_snapshot:历史状态列 + **每只票最新交易日全量数据**。

    1) 历史已有行:仅补 is_st/trade_status 空值(修宇宙静默 no-op)
    2) 每只 code 的 baostock 最新一根 K:全量 upsert(OHLCV/pct/状态/PE/PB/换手)
       —— 缺行则新建,有行则覆盖,保证池内最新交易日数据齐全

    返回 {codes, rows_patched, latest_upserted, latest_date_max, errors}。
    """
    from stockfu.data.baostock_source import BaostockSource
    from stockfu.services.universe import resolve_base_codes

    if codes is None:
        codes = resolve_base_codes("all")
    src = BaostockSource()
    patched = 0
    latest_upserted = 0
    errors = 0
    latest_dates: list[date] = []

    for i, code in enumerate(codes):
        try:
            bars = src.get_kline(code, days)
        except Exception:  # noqa: BLE001
            errors += 1
            continue
        if not bars:
            errors += 1
            continue
        bars = sorted(bars, key=lambda b: b.date)
        by_d = {b.date: b for b in bars}
        last = bars[-1]
        latest_dates.append(last.date)
        # 最新 bar 的前收 = 倒数第二根 close
        prev_close = bars[-2].close if len(bars) >= 2 else None

        with session_scope() as s:
            rows = s.exec(
                select(QuoteSnapshot).where(QuoteSnapshot.asset_code == code)
            ).all()
            have = {r.quote_date: r for r in rows}

            # ① 历史行:只补状态空列(跳过最新日,下面全量写)
            for row in rows:
                if row.quote_date == last.date:
                    continue
                b = by_d.get(row.quote_date)
                if b and _patch_status_only(row, b):
                    patched += 1

            # ② 最新交易日:全量 upsert
            snap = have.get(last.date)
            if snap is None:
                snap = QuoteSnapshot(asset_code=code, quote_date=last.date)
                s.add(snap)
            _apply_bar_full(snap, last, prev_close)
            latest_upserted += 1
            s.commit()

        if (i + 1) % 50 == 0:
            print(
                f"  quote_status {i + 1}/{len(codes)}  "
                f"status_patched={patched}  latest_upserted={latest_upserted}",
                flush=True,
            )

    return {
        "codes": len(codes),
        "rows_patched": patched,
        "latest_upserted": latest_upserted,
        "latest_date_max": max(latest_dates).isoformat() if latest_dates else None,
        "latest_date_min": min(latest_dates).isoformat() if latest_dates else None,
        "errors": errors,
    }


def update_index_benchmark(code: str = "sh000001") -> int:
    """查 index_quote_daily 最新日期 → 拉 gap → 幂等 upsert。

    akshare 指数日线（index_zh_a_hist）走国内直连（no_proxy）。
    返回新增行数。
    """
    akshare_symbol = code[2:]  # sh000001 → 000001
    with session_scope() as s:
        last_row = s.exec(
            select(IndexQuoteDaily).where(
                IndexQuoteDaily.asset_code == code
            ).order_by(IndexQuoteDaily.quote_date.desc()).limit(1)
        ).first()
    last_date = last_row.quote_date if last_row else None
    today = date.today()
    if last_date and last_date >= today:
        return 0
    start = (last_date + timedelta(days=1)).isoformat() if last_date else "1990-01-01"
    end = today.isoformat()
    from stockfu.data.akshare_source import get_index_daily
    rows = get_index_daily(akshare_symbol, start, end)
    if not rows:
        return 0
    n = 0
    with session_scope() as s:
        for r in rows:
            existing = s.exec(select(IndexQuoteDaily).where(
                IndexQuoteDaily.asset_code == code,
                IndexQuoteDaily.quote_date == r["quote_date"],
            )).first()
            if existing:
                continue
            s.add(IndexQuoteDaily(**r))
            n += 1
        s.commit()
    return n


def update_etf_benchmark(code: str) -> int:
    """ETF 日线增量更新:查 etf_quote_daily 最新日期 → 拉 gap → 幂等 upsert。

    akshare fund_etf_hist_em(前复权)走国内直连。板块情绪 compute_sector 依赖代表 ETF
    的 K 线分位,行情拆表后 ETF 历史在 etf_quote_daily(非 quote_snapshot)。返回新增行数。
    范式同 update_index_benchmark。
    """
    from stockfu.data.akshare_source import get_etf_daily
    with session_scope() as s:
        last_row = s.exec(select(EtfQuoteDaily).where(
            EtfQuoteDaily.asset_code == code
        ).order_by(EtfQuoteDaily.quote_date.desc()).limit(1)).first()
    last_date = last_row.quote_date if last_row else None
    today = date.today()
    if last_date and last_date >= today:
        return 0
    start = (last_date + timedelta(days=1)).isoformat() if last_date else "2010-01-01"
    rows = get_etf_daily(code, start, today.isoformat())
    if not rows:
        return 0
    n = 0
    with session_scope() as s:
        for r in rows:
            existing = s.exec(select(EtfQuoteDaily).where(
                EtfQuoteDaily.asset_code == code,
                EtfQuoteDaily.quote_date == r["quote_date"],
            )).first()
            if existing:
                continue
            s.add(EtfQuoteDaily(**r))
            n += 1
        s.commit()
    return n


def run_backfill_benchmark(code: str = "sh000001") -> dict:
    """一次性回补整个指数历史（从最早日期到今天），首次部署用。"""
    from stockfu.data.akshare_source import get_index_daily
    n = 0
    with session_scope() as s:
        existing = s.exec(select(IndexQuoteDaily).where(
            IndexQuoteDaily.asset_code == code
        )).all()
        have_dates = {r.quote_date for r in existing}
    # 全量拉取（1990 至今）
    today = date.today()
    rows = get_index_daily(code[2:], "1990-01-01", today.isoformat())
    new_rows = [r for r in rows if r["quote_date"] not in have_dates]
    if new_rows:
        with session_scope() as s:
            for r in new_rows:
                s.add(IndexQuoteDaily(**r))
            s.commit()
        n = len(new_rows)
    return {"code": code, "total": len(rows), "new": n, "have_before": len(have_dates)}


# 申万一级行业指数(31 个,akshare sw_index_first_info;2021 现行分类口径)。
# 键=裸 6 位指数代码(喂 akshare index_hist_sw),值=行业名(展示/映射用)。单一真源,probes 复用。
SW_INDUSTRIES = {
    "801010": "农林牧渔", "801030": "基础化工", "801040": "钢铁", "801050": "有色金属",
    "801080": "电子", "801880": "汽车", "801110": "家用电器", "801120": "食品饮料",
    "801130": "纺织服饰", "801140": "轻工制造", "801150": "医药生物", "801160": "公用事业",
    "801170": "交通运输", "801180": "房地产", "801200": "商贸零售", "801210": "社会服务",
    "801780": "银行", "801790": "非银金融", "801230": "综合", "801710": "建筑材料",
    "801720": "建筑装饰", "801730": "电力设备", "801890": "机械设备", "801740": "国防军工",
    "801750": "计算机", "801760": "传媒", "801770": "通信", "801950": "煤炭",
    "801960": "石油石化", "801970": "环保", "801980": "美容护理",
}


def backfill_sw_index() -> dict:
    """一次性回补 31 个申万一级行业指数历史日线(akshare index_hist_sw)→ index_quote_daily。

    范式同 run_backfill_benchmark:per-symbol 两段式 session(读已有日期集合→关→拉网络→开新→add→commit)。
    asset_code = f"sw{symbol}";⚠️ 不做 code[2:] 剥前缀(那是 benchmark 为剥 "sh";SW 符号本就裸 6 位)。
    返回 {asset_code: {total, new, have_before}}。
    """
    from stockfu.data.akshare_source import get_sw_index_daily
    summary: dict[str, dict] = {}
    for sym in SW_INDUSTRIES:
        asset_code = f"sw{sym}"
        with session_scope() as s:
            existing = s.exec(select(IndexQuoteDaily).where(
                IndexQuoteDaily.asset_code == asset_code)).all()
            have_dates = {r.quote_date for r in existing}
        rows = get_sw_index_daily(sym)
        new_rows = [r for r in rows if r["quote_date"] not in have_dates]
        if new_rows:
            with session_scope() as s:
                for r in new_rows:
                    s.add(IndexQuoteDaily(**r))
                s.commit()
        summary[asset_code] = {"total": len(rows), "new": len(new_rows),
                               "have_before": len(have_dates)}
    return summary


# 行业 ETF(可交易标的):一行业一只代表 ETF,覆盖主要申万一级行业。键=ETF 代码,值=申万行业名。
# 选流动性较好、上市较早的(2016-2021);探测/轮动复用。代码错或退市的 get_etf_daily 返 [] 自动跳过。
INDUSTRY_ETFS = {
    "512800": "银行", "512690": "食品饮料", "512480": "电子", "512010": "医药生物",
    "512660": "国防军工", "515030": "汽车", "512720": "计算机", "512980": "传媒",
    "515880": "通信", "512400": "有色金属", "515210": "钢铁", "515220": "煤炭",
    "159865": "农林牧渔", "516160": "电力设备", "562500": "机械设备",
    "159996": "家用电器", "512070": "非银金融", "512580": "环保",
}


def backfill_industry_etf() -> dict:
    """一次性回补行业 ETF 历史日线(akshare fund_etf_hist_em,前复权)→ etf_quote_daily。

    范式同 backfill_sw_index;ETF 取数带 start/end(从 2010 至今,覆盖上市以来)。返回 {code: {total,new,have_before}}。
    """
    from stockfu.data.akshare_source import get_etf_daily
    today = date.today()
    summary: dict[str, dict] = {}
    for code in INDUSTRY_ETFS:
        with session_scope() as s:
            existing = s.exec(select(EtfQuoteDaily).where(
                EtfQuoteDaily.asset_code == code)).all()
            have_dates = {r.quote_date for r in existing}
        rows = get_etf_daily(code, "2010-01-01", today.isoformat())
        new_rows = [r for r in rows if r["quote_date"] not in have_dates]
        if new_rows:
            with session_scope() as s:
                for r in new_rows:
                    s.add(EtfQuoteDaily(**r))
                s.commit()
        summary[code] = {"industry": INDUSTRY_ETFS[code],
                         "total": len(rows), "new": len(new_rows),
                         "have_before": len(have_dates)}
    return summary


def run_backfill(days: int) -> dict:
    """回填宽基/行业 ETF + 自选 + 板块自身K线 + 大盘资金流 的历史，供指数/情绪计算。"""
    init_db()
    with session_scope() as s:
        watch = [a.code for a in s.exec(select(Asset)).all()]
    targets = list(dict.fromkeys(INDEX_ETFS + watch))  # 去重保序
    result: dict[str, int] = {}
    for code in targets:
        try:
            result[code] = backfill_kline(code, days)
        except Exception as exc:  # noqa: BLE001
            result[code] = -1
    # 板块自身K线+成交额（同花顺，绕开东财限流；跳过无映射的宽基）+ 大盘资金流历史
    from stockfu.services import backfill as bf
    from stockfu.services.composite import SECTOR_MAP, SECTOR_THS_NAME
    sec_days = max(days, 1460)                 # 板块情绪分位要 4 年历史
    sectors: dict[str, int] = {}
    for name in SECTOR_MAP:
        if not SECTOR_THS_NAME.get(name):      # 宽基无同花顺映射，跳过
            continue
        try:
            sectors[name] = bf.backfill_sector_kline(name, sec_days)
        except Exception as exc:  # noqa: BLE001
            sectors[name] = -1
    try:
        market_flow = bf.backfill_market_fund_flow()
    except Exception:  # noqa: BLE001
        market_flow = -1
    result["sectors"] = sectors
    result["market_flow"] = market_flow
    return result


def ensure_stock_data_and_index(code: str, days: int = 1825) -> dict:
    """单只个股：历史不足则补 K 线 + 抓今日行情 + 算个股三层情绪指数落库。

    供 TUI「加个股即算」与将来 CLI 复用；返回摘要 dict。
    """
    from stockfu.services.composite import compute_stock, save

    with session_scope() as s:
        have = len(s.exec(select(QuoteSnapshot).where(
            QuoteSnapshot.asset_code == code)).all())
    backfilled = backfill_kline(code, days) if have < 60 else 0  # 历史够就不重复拉
    quoted = _upsert_quote(code)
    result = compute_stock(code)
    save(result)
    return {
        "history_before": have,
        "backfilled": backfilled,
        "quoted": quoted,
        "fear": result.get("fear"),
        "greed": result.get("greed"),
        "heat": result.get("heat"),
    }


def _batch_fetch_today(codes: list[str]) -> tuple[list[str], list[str]]:
    """对 codes 逐个 _upsert_quote。返回 (成功, 失败)。"""
    ok, fail = [], []
    for c in codes:
        try:
            (ok if _upsert_quote(c) else fail).append(c)
        except Exception:  # noqa: BLE001
            fail.append(c)
    return ok, fail


def run_scheduled_fetch() -> dict:
    """到 daily_fetch_time 触发：批量抓今日 + 失败按间隔重试 N 次 + 分红/ETF/三层指数。

    重试只针对上一轮失败的 code（已落盘的 _upsert_quote 会秒跳过，双重保险）；
    重试耗尽后剩下的不管（读路径会显示最近一条历史快照）。
    """
    import time as _t

    from stockfu.config import get_fetch_retry_count, get_fetch_retry_interval

    init_db()
    with session_scope() as s:
        codes = [a.code for a in s.exec(select(Asset)).all()]
    targets = list(dict.fromkeys(codes + INDEX_ETFS))

    ok, fail = _batch_fetch_today(targets)
    retries = get_fetch_retry_count()
    for _ in range(retries):
        if not fail:
            break
        # 只对可能恢复的标的等待重试：港美股断连时直接跳过，不白白等待
        if not all(c.startswith(("HK", "US", "au")) for c in fail):
            _t.sleep(get_fetch_retry_interval() * 60)
        ok2, fail = _batch_fetch_today(fail)
        ok.extend(ok2)

    # 三大指数日线落 IndexQuoteDaily（行情拆表后指数在此;读路径 index_quotes_view/share.perf 直读此表）
    for _idx in ("sh000001", "sz399006", "sh000688"):
        try:
            update_index_benchmark(_idx)
        except Exception:  # noqa: BLE001
            pass
    # SECTOR_MAP 代表 ETF 日线增量(板块情绪 compute_sector 依赖;拆表后 ETF 在 etf_quote_daily)
    from stockfu.services.composite import SECTOR_MAP as _SECTOR_ETF_MAP
    for _etf in _SECTOR_ETF_MAP.values():
        try:
            update_etf_benchmark(_etf)
        except Exception:  # noqa: BLE001
            pass
    # 板块当日主力资金流（即时，每日攒历史；落库后供 compute_sector 用）
    from stockfu.services import backfill as bf
    sector_flow = bf.backfill_sector_flow_today()
    # 后半段：分红 / ETF 份额 / 三层指数
    from stockfu.services import composite, dividend as div_svc
    with session_scope() as s:
        all_codes = [a.code for a in s.exec(select(Asset)).all()]
    divs = sum(div_svc.persist_dividends(c) for c in all_codes)
    flows = sum(1 for c in INDEX_ETFS if _upsert_fundflow(c))
    # 三层情绪指数：整体给 120s 超时（个股外部因子可能挂死），超时则只算市场级
    import threading as _th
    _comp_box: dict = {}
    def _run_comp():
        try:
            _comp_box["r"] = composite.compute_all(all_codes)
        except Exception as _e:
            _comp_box["e"] = _e
    _t = _th.Thread(target=_run_comp, daemon=True)
    _t.start()
    _t.join(120)
    if "r" in _comp_box:
        comp = _comp_box["r"]
    else:
        from stockfu.services.snapshot import beijing_today
        print(f"  compute_all 超时(120s)，降级只算市场+板块情绪")
        comp = {}
        comp["market"] = composite.compute_market()
        composite.save(comp["market"])
        for _name, _etf in composite.SECTOR_MAP.items():
            try:
                _r = composite.compute_sector(_etf, _name)
                if _r.get("fear") or _r.get("greed") or _r.get("heat"):
                    comp[f"sector:{_name}"] = _r
                    composite.save(_r)
            except Exception:
                pass
    return {"quotes": len(ok), "retries": retries, "still_failed": len(fail),
            "still_failed_codes": fail[:20], "dividends": divs,
            "fundflow_etfs": flows, "sector_flow": sector_flow,
            "composite_levels": len(comp) if isinstance(comp, dict) else 0}


def start_embedded_server() -> str:
    """后台线程起 uvicorn（daemon，随主进程退出），供 playwright 渲染本进程页面 →
    单进程即可出图发信，无需另开 --serve。--schedule / --test-mail 复用。返回 base_url。"""
    import threading

    import uvicorn
    from stockfu.config import settings

    cfg = uvicorn.Config("stockfu.api.server:app", host=settings.api_host,
                         port=settings.api_port, log_level="warning")
    server = uvicorn.Server(cfg)
    server.install_signal_handlers = lambda: None  # 非主线程禁用信号注册
    threading.Thread(target=server.run, daemon=True, name="stockfu-web").start()
    return f"http://{settings.api_host}:{settings.api_port}"


def run_schedule() -> None:
    """APScheduler 长驻（单进程）：工作日 daily_fetch_time（北京，web 可改）抓行情+算指数；
    邮件已启用且配置完整时，内嵌 uvicorn（无需另开 --serve）+ 到点自动出图发信。

    一条 `python main.py --schedule` 即同时是 web 服务 + 调度器，可直接挂服务器常驻：
    mail job 的 playwright 渲染分享卡片时，访问的就是本进程内嵌的 web 页面。
    """
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    from stockfu.config import (get_daily_fetch_time, get_mail_days, get_mail_enabled,
                                get_mail_time, is_mail_ready)

    init_db()
    sched = BlockingScheduler(timezone="Asia/Shanghai")

    # 内嵌 web（mail job 渲染分享卡片时需要本进程的页面）
    started_web = False

    if get_mail_enabled() and is_mail_ready():
        start_embedded_server()
        started_web = True
        print("✓ 内嵌 web 已启动（供 playwright 渲染分享卡片）")
    else:
        print("· 邮件未启用或未配置完整，跳过内嵌 web（面板配置后重启 --schedule 生效）")

    from stockfu.services.mail import run_mail_job

    def _fetch_then_mail() -> dict:
        """抓取 + 分红/ETF/三层指数 → 全部完后自动发邮件（不等定时）。"""
        result = run_scheduled_fetch()
        # 只要邮件就绪就发，不管个别标的失败（有数据的发，没数据的跳）
        if get_mail_enabled() and is_mail_ready():
            try:
                mail_result = run_mail_job()
                result["mail"] = mail_result
            except Exception as exc:  # noqa: BLE001
                result["mail"] = {"ok": False, "detail": str(exc)}
        return result

    hhmm = get_daily_fetch_time()
    h, m = (int(x) for x in hhmm.split(":"))
    sched.add_job(
        _fetch_then_mail,
        CronTrigger(hour=h, minute=m, day_of_week="mon-fri", timezone="Asia/Shanghai"),
        id="daily", max_instances=1, coalesce=True,
    )
    print(f"✓ 抓取任务：工作日 {hhmm}（北京）抓行情 + 算指数 → 自动发邮件")

    print("调度已启动，Ctrl-C 退出。")
    sched.start()
