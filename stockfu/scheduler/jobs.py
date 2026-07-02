"""每日定时任务：抓行情/分红/ETF份额 → 落库(天级快照) → 算指数。

触发方式：
  python main.py --fetch      # 立即跑一次 run_daily_job
  python main.py --backfill   # 回填关键标的 90 日历史（首次必跑，指数才能算）
  python main.py --schedule   # APScheduler 按 daily_cron 长驻运行
"""
from __future__ import annotations

from datetime import date

from sqlmodel import select

from stockfu.db import init_db, session_scope
from stockfu.models import (Asset, FundFlowSnapshot, QuoteSnapshot)

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
    with session_scope() as s:
        existing = s.exec(select(QuoteSnapshot).where(
            QuoteSnapshot.asset_code == code, QuoteSnapshot.quote_date == tday)).first()
        if existing and existing.close:          # 最近交易日已落盘 → 跳过
            return True
    with session_scope() as s:
        snap = s.exec(select(QuoteSnapshot).where(
            QuoteSnapshot.asset_code == code, QuoteSnapshot.quote_date == tday)).first()
        snap = snap or QuoteSnapshot(asset_code=code, quote_date=tday)
        snap.open, snap.high, snap.low, snap.close = o, h, l, c
        snap.pct_chg, snap.volume, snap.amount = pct, vol, amt
        if q:
            snap.pe, snap.pb, snap.market_cap = q.pe, q.pb, q.market_cap
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


def _persist_index_quote(local_code: str, d, price: float, chg) -> None:
    """落/更 单条指数 quote_snapshot。d=落库日期（today 或日K最后一日）。"""
    with session_scope() as s:
        snap = s.exec(select(QuoteSnapshot).where(
            QuoteSnapshot.asset_code == local_code, QuoteSnapshot.quote_date == d)).first()
        snap = snap or QuoteSnapshot(asset_code=local_code, quote_date=d)
        snap.close = price
        snap.pct_chg = chg
        if snap.id is None:
            s.add(snap)
        s.commit()


def _upsert_index_quotes() -> int:
    """主要指数(上证/创业板/科创50)当日行情落 quote_snapshot。

    多源兜底链（任一层成功就停；不会覆盖已成功写入的 code）：
    1) 东财 batch（ak.stock_zh_index_spot_em）— 最权威，按 上证/深证 分 series
    2) 新浪全量（ak.stock_zh_index_spot_sina）— 单次拉全量；补主源缺失 code
    3) 日K末条（ak.stock_zh_index_daily）— 末源；末条日期若≠today 则标注"滞后"
       并落日K最后一日（index_quotes_view 的日期守卫会让邮件仍显 —，但历史分位
       仍可用）

    修复记录：东财深证端点偶发 Remote end closed connection without response，
    三次重试也不够；之前没单代码兜底会丢当天数据。sina 兜底在断流时仍能取到当日值。
    """
    import time as _t
    from datetime import date as _date

    from stockfu.data.base import direct_connection
    from stockfu.services.snapshot import beijing_today

    with direct_connection():
        import akshare as ak
    today = beijing_today()
    cfg = [("000001", "sh000001"), ("000688", "sh000688"), ("399006", "sz399006")]
    by_em = {em: lo for em, lo in cfg}
    found: dict = {}                              # em_code → (price, pct, source, date)

    # === 1) 主源：东财 batch（按 上证/深证 series）===
    for sym, em_codes in (("上证系列指数", ("000001", "000688")),
                          ("深证系列指数", ("399006",))):
        df = None
        for attempt in range(3):
            try:
                with direct_connection():
                    df = ak.stock_zh_index_spot_em(symbol=sym)
                break
            except Exception as exc:
                if attempt < 2:
                    _t.sleep(2)
                else:
                    print(f"  指数主源({sym})失败: {type(exc).__name__}")
        if df is None:
            continue
        want = set(em_codes)
        for _, r in df.iterrows():
            c = str(r.get("代码", "")).strip()
            if c not in want or c in found:
                continue
            try:
                p, chg = float(r["最新价"]), float(r["涨跌幅"])
            except (TypeError, ValueError, KeyError):
                continue
            found[c] = (p, chg, "东财", today)

    # === 2) 兜底：新浪全量（补缺失 code，不覆盖已找到的）===
    missing = [c for c, _ in cfg if c not in found]
    if missing:
        try:
            with direct_connection():
                df_s = ak.stock_zh_index_spot_sina()
            for c in missing:
                r = df_s[df_s["代码"] == by_em[c]]
                if r.empty:
                    continue
                try:
                    p, chg = float(r.iloc[0]["最新价"]), float(r.iloc[0]["涨跌幅"])
                except (TypeError, ValueError, KeyError):
                    continue
                found[c] = (p, chg, "新浪", today)
        except Exception as exc:
            print(f"  指数新浪兜底失败: {type(exc).__name__}")

    # === 3) 末源：日K末条（极端兜底；可能滞后）===
    missing = [c for c, _ in cfg if c not in found]
    for c in missing:
        try:
            with direct_connection():
                df_k = ak.stock_zh_index_daily(symbol=by_em[c])
            if df_k is None or df_k.empty:
                continue
            last = df_k.iloc[-1]
            last_d = last["date"]
            if hasattr(last_d, "date"):
                last_d = last_d.date()
            prev = df_k.iloc[-2] if len(df_k) >= 2 else None
            p = float(last["close"])
            chg = round((p / float(prev["close"]) - 1) * 100, 2) if prev is not None else None
            tag = "日K" if last_d == today else f"日K滞后{today - last_d}天"
            found[c] = (p, chg, tag, last_d)
        except Exception as exc:
            print(f"  指数日K兜底失败({c}): {type(exc).__name__}")

    # === 4) 落库 + 诊断 ===
    n = 0
    for c, (p, chg, src, d) in found.items():
        try:
            _persist_index_quote(by_em[c], d, p, chg)
            n += 1
        except Exception as exc:
            print(f"  指数落库失败({c}): {type(exc).__name__}")
    parts = [f"{c}={found[c][2]}" for c, _ in cfg if c in found]
    miss = [c for c, _ in cfg if c not in found]
    print(f"  指数落盘: {n}/3" + (f" [{', '.join(parts)}]" if parts else ""))
    if miss:
        print(f"  指数完全失败: {miss}")
    return n


def backfill_kline(code: str, days: int = 90) -> int:
    """拉历史日K灌入 quote_snapshot（首次/补数据）。返回新增条数。"""
    from stockfu.data.manager import get_manager
    bars = sorted(get_manager().get_kline(code, days), key=lambda b: b.date)
    if not bars:
        return 0
    n = 0
    with session_scope() as s:
        have = {q.quote_date for q in s.exec(
            select(QuoteSnapshot).where(QuoteSnapshot.asset_code == code)).all()}
        prev_close = None
        for b in bars:
            if b.date not in have:
                pct = round((b.close / prev_close - 1) * 100, 2) if prev_close else None
                s.add(QuoteSnapshot(
                    asset_code=code, quote_date=b.date, open=b.open, high=b.high,
                    low=b.low, close=b.close, pct_chg=pct, volume=b.volume, amount=b.amount,
                ))
                n += 1
            prev_close = b.close
        s.commit()
    return n


def run_backfill(days: int = 90) -> dict:
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


def run_daily_job() -> dict:
    """每日：行情落库 + 分红落库 + ETF份额落库 + 算指数。"""
    init_db()
    from stockfu.services import indices as indices_svc
    from stockfu.services import dividend as div_svc

    with session_scope() as s:
        codes = [a.code for a in s.exec(select(Asset)).all()]
    key_codes = list(dict.fromkeys(codes + INDEX_ETFS))

    quotes = sum(1 for c in key_codes if _upsert_quote(c))
    divs = sum(div_svc.persist_dividends(c) for c in codes)
    flows = sum(1 for c in INDEX_ETFS if _upsert_fundflow(c))
    # 板块当日主力资金流（即时攒历史）
    from stockfu.services import backfill as bf
    sector_flow = bf.backfill_sector_flow_today()
    indices = indices_svc.compute_and_save()
    # 三层情绪指数（市场 / 个股 / 板块）
    from stockfu.services import composite
    comp = composite.compute_all(codes)
    return {"quotes": quotes, "dividends": divs,
            "fundflow_etfs": flows, "indices": indices,
            "sector_flow": sector_flow, "composite_levels": len(comp)}


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

    # 主要指数当日行情落盘
    idx_n = _upsert_index_quotes()
    print(f"  指数行情落盘: {idx_n}/3（上证/科创50/创业板指）")
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
