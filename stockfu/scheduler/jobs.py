"""每日定时任务：抓行情/分红/ETF份额 → 落库(天级快照) → 算指数。

触发方式：
  python main.py --fetch      # 立即跑一次 run_scheduled_fetch
  python main.py --backfill   # 回填关键标的 90 日历史（首次必跑，指数才能算）
  python main.py --schedule   # APScheduler 按 daily_cron 长驻运行
"""
from __future__ import annotations

import os

from datetime import date, timedelta

from sqlmodel import select

from stockfu.db import init_db, session_scope
from stockfu.models import (
    Asset, EtfQuoteDaily, FundFlowSnapshot, IndexQuoteDaily, QuoteSnapshot,
    SecurityMaster,
)

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


def _is_cn_stock(code: str) -> bool:
    """是否 A 股个股(走 baostock 三复权路径)。排除指数/ETF/港美股。"""
    from stockfu.data.base import detect_market, Market
    if code.startswith(("sh", "sz", "SH", "SZ")):          # 指数(带前缀)
        return False
    if code[:2] in {"15", "50", "51", "52", "56", "58"}:   # ETF
        return False
    if code.startswith(("HK", "US", "au")):                # 港美股/黄金
        return False
    return detect_market(code) == Market.CN


def _fetch_today_via_baostock(code: str, end_date, days: int = 15,
                              budget_s: float | None = None) -> bool:
    """A 股个股当日:全局 baostock session 拉近 N 天三复权 → _apply_and_upsert。

    end_date 为抓取窗口上界(目标交易日,已校验)。写 qfq+raw+hfq(顺带刷新当日
    close_raw,红利分母用)。baostock 全失败(代理池+直连兜底)→ 返回 False;调用方
    据此放弃该票当日(**不降级东财/腾讯**,避免残缺 OHLCV 冒充完整数据)。

    同步执行(2026-08-17 审查 H1 修复):不再由外层 _call_timeout 子线程包裹——
    外层 35s < 内层 fetch_timeout 60s,超时留下的孤儿线程会与下一只票并发共用
    全局裸 TCP socket(响应交错可写错数据)。时长上界改由 budget_s
    (env STOCKFU_BS_FETCH_BUDGET,默认 150s)经 run(deadline=...) 约束:坏代理
    时旋转在预算内进行,超预算即放弃该票、留给重试。
    """
    import time as _time
    from datetime import timedelta as _td
    from stockfu.data.baostock_proxy import ensure_baostock_login, get_global_session
    from stockfu.scheduler.backfill_adj_prices import _apply_and_upsert
    from stockfu.services.quote_writer import _coerce_date

    if not ensure_baostock_login():       # 首只票 boot 全局 session;后续秒返回
        return False
    sess = get_global_session()
    if sess is None:
        return False
    if budget_s is None:
        budget_s = float(os.environ.get("STOCKFU_BS_FETCH_BUDGET", "150"))
    deadline = _time.monotonic() + budget_s
    end_d = _coerce_date(end_date)
    start = (end_d - _td(days=days + 5)).isoformat()
    end = end_d.isoformat()
    try:
        triple = sess.fetch_kline_triple(code, start, end, deadline=deadline)
    except Exception:  # noqa: BLE001
        return False
    if not any(triple.values()):
        return False
    return _apply_and_upsert(code, triple, preserve_qfq=False, cap_date=end) > 0


def _current_index_fetch_codes(target_date, index_codes=None) -> list[str]:
    """返回目标日沪深 300 + 中证 500 的有效成分代码。

    成分快照定义“当前属于指数”，``security_master`` 再负责排除已退市、
    未上市或状态非正常的代码。主数据缺失时不武断丢弃成分，方便先补行情再
    补主数据；当前正式 300+500 快照应全部有对应主数据。
    """
    from stockfu.services.index_universe import current_member_codes, HISTORICAL_INDEX_CODES
    from stockfu.services.quote_writer import _coerce_date

    td = _coerce_date(target_date)
    idx = tuple(index_codes) if index_codes else HISTORICAL_INDEX_CODES
    codes = current_member_codes(td, idx)
    if not codes:
        return []
    with session_scope() as s:
        masters = {
            row.code: row for row in s.exec(
                select(SecurityMaster).where(SecurityMaster.code.in_(codes))
            ).all()
        }
    inactive = {
        code for code, row in masters.items()
        if (row.list_date and row.list_date > td)
        or (row.delist_date and row.delist_date <= td)
        or str(row.status or "").strip() not in {"", "1"}
    }
    if inactive:
        print(
            f"  [fetch-universe] skip inactive={len(inactive)} "
            f"codes={sorted(inactive)[:8]}", flush=True,
        )
    return [code for code in codes if code not in inactive]


def fetch_universe_quotes(target_date, *, index_codes=None, progress_every: int = 100) -> dict:
    """补全市场成分当日行情:对指数时点成员逐只抓 baostock 三复权写 quote_snapshot。

    与 run_scheduled_fetch(只抓 asset 自选 ~45 只)互补:本函数抓 hs300+zz500 时点
    成分(~800 只活跃大盘股),把行情补到全成分口径。退市/停牌当日 baostock 返回
    empty,_fetch_today_via_baostock 自然返回 False 跳过,不阻塞、不污染数据。

    主线程串行抓取:绕开 _upsert_quote 的 _call_timeout 子线程——baostock 全局 socket
    在双层子线程里登录态保不住,会陷入 login 循环(实测每只重 login、不入库)。

    target_date: 目标交易日(str 或 date;由 validate_ingest_date 校验,非法即 raise)。
    index_codes: 默认 HISTORICAL_INDEX_CODES(沪深300+中证500)。
    返回 {total, ok, fail, elapsed_sec}。
    """
    import time as _t
    from stockfu.services.quote_writer import validate_ingest_date
    from stockfu.data.baostock_proxy import ensure_baostock_login

    init_db()
    td = validate_ingest_date(target_date)
    from stockfu.services.index_universe import HISTORICAL_INDEX_CODES
    idx = tuple(index_codes) if index_codes else HISTORICAL_INDEX_CODES
    codes = _current_index_fetch_codes(td, idx)
    with session_scope() as s:
        existing = s.exec(select(QuoteSnapshot).where(
            QuoteSnapshot.quote_date == td,
            QuoteSnapshot.asset_code.in_(codes),
        )).all()
    fresh = {
        row.asset_code for row in existing
        if (row.close_qfq is not None and row.close_qfq > 0)
        or (row.close is not None and row.close > 0)
    }
    pending = [code for code in codes if code not in fresh]
    skipped = len(codes) - len(pending)
    if not pending:
        print(
            f"=== [fetch-universe] {td} members={len(codes)} "
            f"pending=0 skipped={skipped} ===",
            flush=True,
        )
        return {
            "total": len(codes), "pending": 0, "skipped": skipped,
            "ok": 0, "fail": 0, "elapsed_sec": 0.0,
        }
    if not ensure_baostock_login():
        return {"total": len(codes), "pending": len(pending), "skipped": skipped,
                "ok": 0, "fail": len(pending),
                "elapsed_sec": 0.0, "error": "baostock login failed"}

    print(
        f"=== [fetch-universe] {td} members={len(codes)} "
        f"pending={len(pending)} skipped={skipped} indices={idx} ===",
        flush=True,
    )
    t0 = _t.time(); ok = fail = 0
    for i, code in enumerate(pending, 1):
        try:
            if _fetch_today_via_baostock(code, td):
                ok += 1
            else:
                fail += 1
        except Exception:  # noqa: BLE001
            fail += 1
        if progress_every and i % progress_every == 0:
            print(f"  universe {td} {i}/{len(pending)} ok={ok} fail={fail} "
                  f"{_t.time() - t0:.0f}s", flush=True)
    elapsed = _t.time() - t0
    print(f"=== [fetch-universe] {td} done ok={ok} fail={fail} {elapsed:.0f}s ===",
          flush=True)
    return {
        "total": len(codes), "pending": len(pending), "skipped": skipped,
        "ok": ok, "fail": fail, "elapsed_sec": round(elapsed, 1),
    }


def _upsert_quote(code: str, target_date=None, timeout: float = 35) -> bool:
    """拉最近交易日行情落库(按行情表路由:个股/ETF/指数全覆盖)。

    target_date: 目标交易日(已校验)——baostock 抓取窗口上界 + manager 写入 cap。
                 None（读路径按需触发）→ 用今天；quote_date 仍取源 bar.date。
    A 股个股 → baostock 三复权(_fetch_today_via_baostock):全字段,顺带 close_raw。
               baostock 全失败即放弃该票当日,**不降级东财/腾讯**(它们缺 pe/pb/
               状态,残缺 OHLCV 会冒充完整数据)。
    ETF/指数 → update_etf_benchmark / update_index_benchmark(各自 canonical 表)。
               2026-08-17 修复:此前 ETF/指数漏到 manager 路径会写 quote_snapshot
               错表(DB 曾留 510300/510500 孤儿行),指数还可能被 normalize 成
               000001 拿到平安银行行情。本函数是读路径(portfolio/snapshot)与
               Web ensure(routes.ensure_stock_data_and_index)的公共入口,在此
               分流可一处收口三条触发链。
    港美股/黄金 → _upsert_quote_via_manager(多源)。
    timeout: manager 路径单个标的超时秒数(baostock 路径同步串行,由内层
              fetch_timeout/deadline 兜底,不再套线程超时——防孤儿线程与
              全局 socket 并发,见 _fetch_today_via_baostock)。
    """
    from stockfu.models import EtfQuoteDaily, IndexQuoteDaily
    from stockfu.services.factors import quote_model_for

    model = quote_model_for(code)
    if model is EtfQuoteDaily:
        try:
            update_etf_benchmark(code, target_date or date.today())
            return True
        except Exception:  # noqa: BLE001
            return False
    if model is IndexQuoteDaily:
        try:
            update_index_benchmark(code, target_date or date.today())
            return True
        except Exception:  # noqa: BLE001
            return False
    if _is_cn_stock(code):
        end = target_date or date.today()
        return _fetch_today_via_baostock(code, end)
    return _upsert_quote_via_manager(code, target_date, timeout)


def _upsert_quote_via_manager(code: str, target_date=None, timeout: float = 35) -> bool:
    """多源 manager 路径(指数/港美股):get_kline+get_quote → _apply_bar_full(qfq)。

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
        from stockfu.services.quote_writer import _coerce_date
        from stockfu.services.snapshot import latest_trade_date
        tday = _coerce_date(target_date) if target_date else latest_trade_date()
        o, h, l, c, vol, amt = q.open, q.high, q.low, q.price, q.volume, q.amount
        pct = q.pct_chg
    else:
        return False
    from stockfu.services.quote_writer import (
        QuotePayload, WritePolicy, upsert_quote_snapshot,
    )
    with session_scope() as s:
        existing = s.exec(select(QuoteSnapshot).where(
            QuoteSnapshot.asset_code == code, QuoteSnapshot.quote_date == tday)).first()
        # OHLCV 已有 → 仅补空状态列；否则全量写入（含状态/估值）
        pol = WritePolicy.PATCH_STATUS if (existing and existing.close) else WritePolicy.FULL_QFQ
        bar = None
        extras: dict = {}
        if bars:
            bar = bars[-1]                          # 全字段（含状态/估值）
        elif q:
            from types import SimpleNamespace
            bar = SimpleNamespace(open=o, high=h, low=l, close=c, volume=vol,
                                  amount=amt, pct_chg=pct, pe=q.pe, pb=q.pb,
                                  turnover=None, trade_status=None, is_st=None)
        # 实时 quote 的 pe/pb/market_cap 始终补（bar 可能缺）
        if q:
            if q.pe is not None:
                extras["pe"] = q.pe
            if q.pb is not None:
                extras["pb"] = q.pb
            if q.market_cap is not None:
                extras["market_cap"] = q.market_cap
        if bar is not None:
            upsert_quote_snapshot(
                s, code, {tday: QuotePayload(qfq=bar, policy=pol, extras=extras)},
                policy=WritePolicy.FULL_QFQ, cap_date=target_date or tday,
            )
        if q and q.name:                          # 回填 Asset.name
            a = s.get(Asset, code)
            if a and not a.name:
                a.name = q.name
        s.commit()
    return True


def clean_quote_snapshots() -> dict:
    """清理 quote_snapshot 的两类脏行，删除安全（真实数据保留在他表/他日）：

    1. quote_date 不在 A 股交易日历（周末/节假日错标，非交易日抓取产物）；
    2. 代码路由不属于本表的孤儿行——ETF/指数代码（2026-08-17 审查：读路径
       _upsert_quote 曾把 ETF 写进 quote_snapshot，510300/510500 各留 15 行；
       这些日期在 etf_quote_daily/index_quote_daily 均有正主数据）。
    """
    from stockfu.models import EtfQuoteDaily, IndexQuoteDaily
    from stockfu.services.factors import quote_model_for
    from stockfu.services.snapshot import _trade_calendar
    cal = _trade_calendar()
    if not cal:
        return {"deleted": 0, "note": "交易日历不可用，跳过"}
    deleted = 0
    with session_scope() as s:
        # 错表孤儿行的安全护栏：同 (code, date) 在正确表已有正主数据才删。
        covered: dict[type, set] = {}
        orphans = [r for r in s.exec(select(QuoteSnapshot)).all()
                   if quote_model_for(r.asset_code) is not QuoteSnapshot]
        for model in (EtfQuoteDaily, IndexQuoteDaily):
            codes = {r.asset_code for r in orphans
                     if quote_model_for(r.asset_code) is model}
            if codes:
                covered[model] = {
                    (row.asset_code, row.quote_date)
                    for row in s.exec(select(model).where(
                        model.asset_code.in_(codes))).all()
                }
        for r in s.exec(select(QuoteSnapshot)).all():
            model = quote_model_for(r.asset_code)
            wrong_table = model is not QuoteSnapshot
            if wrong_table:
                if (r.asset_code, r.quote_date) not in covered.get(model, set()):
                    continue   # 正确表缺该日数据 → 保留，不冒险删唯一记录
            elif r.quote_date in cal:
                continue       # 正常行
            s.delete(r)
            deleted += 1
        s.commit()
    return {"deleted": deleted}


def _upsert_fundflow(code: str, snap_date) -> bool:
    from stockfu.data.manager import get_manager
    from stockfu.services.quote_writer import _coerce_date
    ff = get_manager().get_etf_fund_flow(code)
    if not ff:
        return False
    d = _coerce_date(snap_date)   # 用目标交易日盖章，不再用 date.today()（凌晨防错标）
    with session_scope() as s:
        snap = s.exec(select(FundFlowSnapshot).where(
            FundFlowSnapshot.etf_code == code, FundFlowSnapshot.snap_date == d)).first()
        snap = snap or FundFlowSnapshot(etf_code=code, snap_date=d)
        snap.nav, snap.shares_outstanding = ff.get("nav"), ff.get("shares")
        snap.net_inflow = ff.get("amount")
        if snap.id is None:
            s.add(snap)
        s.commit()
    return True


# 单行写入叶子（_apply_bar_full / _patch_status_only / _bar_pct）已移至
# stockfu/services/quote_writer.py；本模块所有行情落库改走 writer 收口。


def backfill_kline(code: str, days: int = 90) -> int:
    """拉历史日K灌入行情表（首次/补数据）。返回新增+补丁条数。

    - **ETF**(15/5x 开头):走 get_etf_daily **前复权** → etf_quote_daily
      (禁止 baostock/个股链,避免不复权污染)
    - 个股:manager.get_kline → quote_snapshot
      - 缺失日:全字段插入
      - 已有历史日:仅补空状态列
      - **最新一根 bar**:始终全量覆盖(OHLCV+状态+估值)
    """
    from stockfu.services.factors import quote_model_for
    # cap 锚点统一为「已收盘交易日」：抓取源盘中会返回当日未收盘 partial bar，
    # 以数据自身最大日当 cap 会把它当完整收盘行入库（2026-08-24 审查 H1）。
    from stockfu.services.quote_writer import latest_closed_trade_day
    cap = latest_closed_trade_day()
    if quote_model_for(code) is EtfQuoteDaily:
        from stockfu.data.akshare_source import get_etf_daily
        start = (date.today() - timedelta(days=days + 40)).isoformat()
        rows = get_etf_daily(code, start, cap.isoformat())
        return _upsert_etf_rows(code, rows, cap_date=cap)

    from stockfu.data.manager import get_manager
    from stockfu.services.quote_writer import (
        QuotePayload, WritePolicy, upsert_quote_snapshot,
    )
    bars = sorted(get_manager().get_kline(code, days), key=lambda b: b.date)
    if not bars:
        return 0
    bars = [b for b in bars if b.date <= cap]
    if not bars:
        return 0
    latest_d = bars[-1].date
    # 最新日全量刷新；已有历史日仅补状态；缺失日全量插入（funnel 内 PATCH+新行→FULL）
    payload = {
        b.date: QuotePayload(
            qfq=b,
            policy=WritePolicy.FULL_QFQ if b.date == latest_d else WritePolicy.PATCH_STATUS,
        )
        for b in bars
    }
    with session_scope() as s:
        n = upsert_quote_snapshot(
            s, code, payload, policy=WritePolicy.PATCH_STATUS, cap_date=cap)
        s.commit()
    return n


def backfill_quote_status(codes: list[str] | None = None, days: int = 2000,
                          *, refresh: bool = False) -> dict:
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
    from stockfu.services.backfill_checkpoint import mark_item, pending_items
    scope = f"v1:{days}:{date.today().isoformat()}"
    pending, skipped = pending_items("quote_status", scope, codes, refresh=refresh)
    src = BaostockSource()
    patched = 0
    latest_upserted = 0
    errors = 0
    latest_dates: list[date] = []

    print(f"quote_status checkpoint 跳过:{skipped};待补:{len(pending)};refresh={refresh}",
          flush=True)
    for i, code in enumerate(pending):
        try:
            bars = src.get_kline(code, days)
        except Exception as exc:  # noqa: BLE001
            errors += 1
            mark_item("quote_status", scope, code, success=False,
                      error=f"{type(exc).__name__}: {exc}")
            continue
        if not bars:
            errors += 1
            mark_item("quote_status", scope, code, success=False, error="empty bars")
            continue
        bars = sorted(bars, key=lambda b: b.date)
        # 盘中源可能带当日 partial bar：统一按已收盘交易日截断（审查 H1）。
        from stockfu.services.quote_writer import latest_closed_trade_day as _lctd
        _cap = _lctd()
        bars = [b for b in bars if b.date <= _cap]
        if not bars:
            errors += 1
            mark_item("quote_status", scope, code, success=False,
                      error="no closed-day bars")
            continue
        by_d = {b.date: b for b in bars}
        last = bars[-1]
        latest_dates.append(last.date)

        from stockfu.services.quote_writer import (
            QuotePayload, WritePolicy, upsert_quote_snapshot,
        )
        with session_scope() as s:
            rows = s.exec(
                select(QuoteSnapshot).where(QuoteSnapshot.asset_code == code)
            ).all()
            have = {r.quote_date: r for r in rows}

            # 最新日全量 upsert；已有历史日仅补状态；库内无的历史日不插入(本任务不补K线)
            payload = {}
            for d, b in by_d.items():
                if d == last.date:
                    payload[d] = QuotePayload(qfq=b, policy=WritePolicy.FULL_QFQ)
                elif d in have:
                    payload[d] = QuotePayload(qfq=b, policy=WritePolicy.PATCH_STATUS)
            written = upsert_quote_snapshot(
                s, code, payload, policy=WritePolicy.PATCH_STATUS, cap_date=_cap)
            latest_upserted += 1
            patched += max(0, written - 1)
            s.commit()
        mark_item("quote_status", scope, code, success=True)

        if (i + 1) % 50 == 0:
            print(
                f"  quote_status {i + 1}/{len(pending)}  "
                f"status_patched={patched}  latest_upserted={latest_upserted}",
                flush=True,
            )

    return {
        "codes": len(codes),
        "skipped": skipped,
        "pending": len(pending),
        "rows_patched": patched,
        "latest_upserted": latest_upserted,
        "latest_date_max": max(latest_dates).isoformat() if latest_dates else None,
        "latest_date_min": min(latest_dates).isoformat() if latest_dates else None,
        "errors": errors,
    }


def update_index_benchmark(code: str = "sh000001", target_date=None) -> int:
    """查 index_quote_daily 最新日期 → 拉 gap → 幂等 upsert。

    target_date: 目标交易日(已校验)，窗口上界 + cap；None→今天(兼容 backfill 调用)。
    akshare 指数日线（index_zh_a_hist）走国内直连（no_proxy）。
    返回新增行数。
    """
    from stockfu.services.quote_writer import _coerce_date, latest_closed_trade_day
    akshare_symbol = code[2:]  # sh000001 → 000001
    # None→已收盘最近交易日(原裸 today:盘中跑会拉当日 partial,审查 M2)
    end_d = _coerce_date(target_date) if target_date else latest_closed_trade_day()
    with session_scope() as s:
        last_row = s.exec(
            select(IndexQuoteDaily).where(
                IndexQuoteDaily.asset_code == code
            ).order_by(IndexQuoteDaily.quote_date.desc()).limit(1)
        ).first()
    last_date = last_row.quote_date if last_row else None
    if last_date and last_date >= end_d:
        return 0
    start = (last_date + timedelta(days=1)).isoformat() if last_date else "1990-01-01"
    end = end_d.isoformat()
    from stockfu.data.akshare_source import get_index_daily
    from stockfu.services.quote_writer import upsert_index_daily
    rows = get_index_daily(akshare_symbol, start, end)
    if not rows:
        return 0
    with session_scope() as s:
        n = upsert_index_daily(s, code, rows, cap_date=end_d, overwrite=False)
        s.commit()
    return n


def update_etf_benchmark(code: str, target_date=None) -> int:
    """ETF 日线增量更新:查 etf_quote_daily 最新日期 → 拉 gap → 幂等 upsert。

    target_date: 目标交易日(已校验)，窗口上界 + cap；None→今天(兼容 backfill 调用)。
    get_etf_daily(**前复权 qfq**：东财 fund_etf_hist_em → 腾讯 qfq 兜底)。
    板块情绪 compute_sector 依赖代表 ETF 的 K 线分位;行情拆表后 ETF 历史在
    etf_quote_daily(非 quote_snapshot)。返回新增+覆盖行数。
    """
    from stockfu.data.akshare_source import get_etf_daily
    from stockfu.services.quote_writer import _coerce_date, latest_closed_trade_day
    # None→已收盘最近交易日(原裸 today:盘中跑会拉当日 partial,审查 M2)
    end_d = _coerce_date(target_date) if target_date else latest_closed_trade_day()
    with session_scope() as s:
        last_row = s.exec(select(EtfQuoteDaily).where(
            EtfQuoteDaily.asset_code == code
        ).order_by(EtfQuoteDaily.quote_date.desc()).limit(1)).first()
    last_date = last_row.quote_date if last_row else None
    if last_date and last_date >= end_d:
        return 0
    # 有历史时从最近若干交易日重拉,覆盖最新日(前复权基准随分红/拆分会变)
    if last_date:
        start = (last_date - timedelta(days=14)).isoformat()
    else:
        start = "2010-01-01"
    rows = get_etf_daily(code, start, end_d.isoformat())
    if not rows:
        return 0
    return _upsert_etf_rows(code, rows, cap_date=end_d)


def backfill_benchmark_tr(*, csi_symbol: str = "H00300",
                          internal_code: str = "sh000300_tr") -> dict:
    """回补沪深300全收益指数(H00300)→ index_quote_daily(内部码 sh000300_tr)。

    指标基准同口径用(2026-08-17 审查 #5):策略净值 qfq 含分红再投,对照价格
    指数会把 excess 系统性高估约基准股息率(~2%/年)。V2 引擎按
    ``{benchmark_code}_tr`` 约定自动取本表数据,basis 记入 manifest。
    增量更新(查最新日→拉 gap);cap=已收盘最近交易日(盘中不写当日,防 partial)。
    """
    from stockfu.data.akshare_source import get_csindex_daily
    from stockfu.services.quote_writer import (
        latest_closed_trade_day, upsert_index_daily,
    )

    cap = latest_closed_trade_day()
    with session_scope() as s:
        last_row = s.exec(select(IndexQuoteDaily).where(
            IndexQuoteDaily.asset_code == internal_code
        ).order_by(IndexQuoteDaily.quote_date.desc()).limit(1)).first()
    last_date = last_row.quote_date if last_row else None
    if last_date and last_date >= cap:
        return {"upserted": 0, "up_to": last_date.isoformat(), "note": "已是最新"}
    start = (last_date + timedelta(days=1)).isoformat() if last_date else "2005-01-01"
    rows = get_csindex_daily(csi_symbol, start, cap.isoformat(),
                             asset_code=internal_code)
    if not rows:
        return {"upserted": 0, "up_to": last_date.isoformat() if last_date else "无",
                "note": "源无数据(网络/接口失败?)"}
    with session_scope() as s:
        n = upsert_index_daily(s, internal_code, rows, cap_date=cap, overwrite=False)
        s.commit()
    return {"upserted": n, "up_to": cap.isoformat(),
            "range": [start, cap.isoformat()]}


def run_backfill_benchmark(code: str = "sh000001") -> dict:
    """一次性回补整个指数历史（从最早日期到已收盘最近交易日），首次部署用。"""
    from stockfu.data.akshare_source import get_index_daily
    n = 0
    with session_scope() as s:
        existing = s.exec(select(IndexQuoteDaily).where(
            IndexQuoteDaily.asset_code == code
        )).all()
        have_dates = {r.quote_date for r in existing}
    # 全量拉取（1990 至已收盘最近交易日;盘中不写当日 partial,审查 M2）
    from stockfu.services.quote_writer import latest_closed_trade_day, upsert_index_daily
    cap_day = latest_closed_trade_day()
    rows = get_index_daily(code[2:], "1990-01-01", cap_day.isoformat())
    with session_scope() as s:
        n = upsert_index_daily(s, code, rows, cap_date=cap_day, overwrite=False)
        s.commit()
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


def backfill_sw_index(*, refresh: bool = False) -> dict:
    """一次性回补 31 个申万一级行业指数历史日线(akshare index_hist_sw)→ index_quote_daily。

    范式同 run_backfill_benchmark:per-symbol 两段式 session(读已有日期集合→关→拉网络→开新→add→commit)。
    asset_code = f"sw{symbol}";⚠️ 不做 code[2:] 剥前缀(那是 benchmark 为剥 "sh";SW 符号本就裸 6 位)。
    返回 {asset_code: {total, new, have_before}}。
    """
    from stockfu.data.akshare_source import get_sw_index_daily
    from stockfu.services.quote_writer import upsert_index_daily
    from stockfu.services.backfill_checkpoint import mark_item, pending_items
    summary: dict[str, dict] = {}
    symbols = list(SW_INDUSTRIES)
    pending, skipped = pending_items("sw_index", "v1:all", symbols, refresh=refresh)
    print(f"sw_index checkpoint 跳过:{skipped};待补:{len(pending)};refresh={refresh}", flush=True)
    for sym in pending:
        asset_code = f"sw{sym}"
        try:
            with session_scope() as s:
                existing = s.exec(select(IndexQuoteDaily).where(
                    IndexQuoteDaily.asset_code == asset_code)).all()
                have_dates = {r.quote_date for r in existing}
            rows = get_sw_index_daily(sym)
            if not rows:
                raise RuntimeError("empty rows")
            cap = max(r["quote_date"] for r in rows)
            with session_scope() as s:
                new_n = upsert_index_daily(s, asset_code, rows, cap_date=cap, overwrite=False)
                s.commit()
            mark_item("sw_index", "v1:all", sym, success=True)
            summary[asset_code] = {"total": len(rows), "new": new_n,
                                   "have_before": len(have_dates)}
        except Exception as exc:  # noqa: BLE001
            mark_item("sw_index", "v1:all", sym, success=False,
                      error=f"{type(exc).__name__}: {exc}")
            summary[asset_code] = {"error": f"{type(exc).__name__}: {exc}"}
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

# 动量/红利等策略池中、不在 INDEX/INDUSTRY 的补充 ETF(与历史 27 只池对齐)。
EXTRA_ETFS = ["512710", "515450", "515650", "588870"]


def etf_universe_codes() -> list[str]:
    """全量 ETF 池:INDEX + INDUSTRY + SECTOR_MAP + EXTRA + asset.fund_etf。"""
    from stockfu.services.composite import SECTOR_MAP
    codes: list[str] = (
        list(INDEX_ETFS) + list(INDUSTRY_ETFS)
        + list(SECTOR_MAP.values()) + list(EXTRA_ETFS)
    )
    with session_scope() as s:
        for a in s.exec(select(Asset).where(Asset.asset_type == "fund_etf")).all():
            if a.code:
                codes.append(a.code)
    # 去重保序
    return list(dict.fromkeys(codes))


def clear_etf_data() -> dict:
    """清空全部 ETF 相关行情/份额表(不复权污染后重灌前置)。

    表: etf_quote_daily / fundflow_snapshot / etf_fundflow。
    不删 asset 自选、不碰个股 quote_snapshot。
    """
    from sqlalchemy import text
    from stockfu.db import engine

    out: dict[str, int] = {}
    with engine.begin() as conn:
        for table in ("etf_quote_daily", "fundflow_snapshot", "etf_fundflow"):
            try:
                cnt = int(conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0)
            except Exception:  # noqa: BLE001
                out[table] = -1  # type: ignore[assignment]
                continue
            try:
                conn.execute(text(f"DELETE FROM {table}"))
                out[table] = cnt
            except Exception as exc:  # noqa: BLE001
                out[table] = f"err:{exc}"  # type: ignore[assignment]
    return out


def _upsert_etf_rows(code: str, rows: list[dict], *, cap_date=None) -> int:
    """将 get_etf_daily 行 upsert 进 etf_quote_daily(有则覆盖 OHLC,无则插入)。

    前复权序列在分红/拆分后历史价会整体平移,覆盖保证库内口径一致。
    cap_date 未传时取行内最大日(兼容旧行为;fetch 路径传目标日做日期上限保证)。
    返回写入(新增+更新)行数。
    """
    from stockfu.services.quote_writer import upsert_etf_daily
    if not rows:
        return 0
    cap = cap_date or max(r["quote_date"] for r in rows)
    with session_scope() as s:
        n = upsert_etf_daily(s, code, rows, cap_date=cap)
        s.commit()
    return n


def backfill_etf_quotes(
    codes: list[str] | None = None,
    start: str = "2010-01-01",
    sleep_s: float = 0.6,
    *,
    refresh: bool = False,
) -> dict:
    """全量回补 ETF 日线(**前复权**)→ etf_quote_daily。

    主源东财 qfq(重试)→ 腾讯 qfq 兜底。对已有日期覆盖 OHLC(非仅补缺),
    避免不复权残片与前复权历史拼成断档。
    codes 默认 etf_universe_codes()。
    返回 {code: {total, written, min, max, source_hint}}。
    """
    import time as _t
    from stockfu.data.akshare_source import get_etf_daily
    from stockfu.services.backfill_checkpoint import mark_item, pending_items
    from stockfu.services.quote_writer import latest_closed_trade_day

    today = date.today()
    # cap 锚点=已收盘交易日：东财盘中返回当日 partial 日线，行内最大日当 cap
    # 会把半截 OHLC 当完整收盘行入库（2026-08-24 审查 H1）。
    cap = latest_closed_trade_day()
    pool = codes if codes is not None else etf_universe_codes()
    scope = f"v1:{start}:{today.isoformat()}"
    pending, skipped = pending_items("etf_quotes", scope, pool, refresh=refresh)
    print(f"etf_quotes checkpoint 跳过:{skipped};待补:{len(pending)};refresh={refresh}", flush=True)
    summary: dict[str, dict] = {}
    for i, code in enumerate(pending):
        try:
            rows = get_etf_daily(code, start, cap.isoformat())
            if not rows:
                raise RuntimeError("empty rows")
            written = _upsert_etf_rows(code, rows, cap_date=cap)
            mark_item("etf_quotes", scope, code, success=True)
        except Exception as exc:  # noqa: BLE001
            mark_item("etf_quotes", scope, code, success=False,
                      error=f"{type(exc).__name__}: {exc}")
            summary[code] = {"error": f"{type(exc).__name__}: {exc}"}
            print(f"  [{i+1}/{len(pending)}] {code}: failed {exc}")
            continue
        hint = "empty"
        if rows:
            # 东财通时通常 >900 根;腾讯兜底约 ≤801
            hint = "em_or_deep" if len(rows) > 900 else "likely_tencent_shallow"
        summary[code] = {
            "total": len(rows),
            "written": written,
            "min": str(rows[0]["quote_date"]) if rows else None,
            "max": str(rows[-1]["quote_date"]) if rows else None,
            "source_hint": hint,
        }
        print(f"  [{i+1}/{len(pending)}] {code}: n={len(rows)} written={written} "
              f"{summary[code]['min']}→{summary[code]['max']} ({hint})")
        if i + 1 < len(pending) and sleep_s > 0:
            _t.sleep(sleep_s)
    return summary


def backfill_industry_etf(*, refresh: bool = False) -> dict:
    """一次性回补行业 ETF 历史日线(**前复权**)→ etf_quote_daily。

    走 backfill_etf_quotes(仅 INDUSTRY_ETFS);全池请用 backfill_etf_quotes()。
    """
    return backfill_etf_quotes(list(INDUSTRY_ETFS), refresh=refresh)


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
        except Exception:  # noqa: BLE001
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
        except Exception:  # noqa: BLE001
            sectors[name] = -1
    try:
        market_flow = bf.backfill_market_fund_flow()
    except Exception:  # noqa: BLE001
        market_flow = -1
    result["sectors"] = sectors
    result["market_flow"] = market_flow
    return result


def ensure_stock_data_and_index(code: str, days: int = 1825, target_date=None) -> dict:
    """单只个股：历史不足则补 K 线 + 抓行情(截至 target_date) + 算个股三层情绪指数落库。

    target_date: 目标交易日；None（Web 按需触发）→ 已收盘的最近交易日(过校验)。
    供 Web「加个股即算」与 CLI 复用；返回摘要 dict。
    """
    from stockfu.services.composite import compute_stock, save
    from stockfu.services.quote_writer import (
        latest_closed_trade_day, validate_ingest_date,
    )

    td = (validate_ingest_date(target_date) if target_date
          else validate_ingest_date(latest_closed_trade_day()))
    with session_scope() as s:
        have = len(s.exec(select(QuoteSnapshot).where(
            QuoteSnapshot.asset_code == code)).all())
    backfilled = backfill_kline(code, days) if have < 60 else 0  # 历史够就不重复拉
    quoted = _upsert_quote(code, td)
    result = compute_stock(code, td)
    save(result, td)
    return {
        "history_before": have,
        "backfilled": backfilled,
        "quoted": quoted,
        "fear": result.get("fear"),
        "greed": result.get("greed"),
        "heat": result.get("heat"),
    }


def _batch_fetch_today(codes: list[str], target_date=None) -> tuple[list[str], list[str]]:
    """对 codes 逐个抓行情(截至 target_date)。路由全部由 _upsert_quote 收口:
    ETF→etf_quote_daily、指数→index_quote_daily、A股个股→baostock 三复权、
    其余→manager 多源。ETF 源失败返回 False → 进 fail 列表参与重试
    (2026-08-17 修复:此前 ETF 分支无条件计 ok,源失败被静默吞掉)。"""
    ok, fail = [], []
    for c in codes:
        try:
            (ok if _upsert_quote(c, target_date) else fail).append(c)
        except Exception:  # noqa: BLE001
            fail.append(c)
    return ok, fail


def _current_watch_codes() -> list[str]:
    """返回当前自选/追踪资产。

    ``asset`` 同时保留历史回测资产（``is_watch=False``），不能把整张表
    当成每日行情抓取清单；否则退市/历史 PT、ST 代码会在 baostock 上反复
    返回空结果并拖慢整轮重试。
    """
    with session_scope() as s:
        return list(s.exec(
            select(Asset.code).where(Asset.is_watch == True)  # noqa: E712
        ).all())


def _call_timeout(fn, timeout: float, label: str = "", default=None):
    """在守护线程跑 fn，超时返回 default（不杀线程，但主流程不阻塞）。"""
    import threading
    box: dict = {}

    def _run():
        try:
            box["r"] = fn()
        except Exception as e:  # noqa: BLE001
            box["e"] = e

    t = threading.Thread(target=_run, daemon=True, name=f"to-{label or 'job'}"[:40])
    t.start()
    t.join(timeout)
    if t.is_alive():
        print(f"  [timeout {timeout:.0f}s] {label}", flush=True)
        return default
    if "e" in box:
        print(f"  [err] {label}: {type(box['e']).__name__}: {box['e']}", flush=True)
        return default
    return box.get("r", default)


def run_scheduled_fetch(target_date) -> dict:
    """批量抓行情(截至 target_date) + 失败重试 + 分红/ETF/三层指数。

    target_date: 目标交易日(**必填**)。非法(未来/未收盘/非交易日)→ raise ValueError。
                 所有行情窗口上界、快照盖章(资金流/情绪/板块资金流)统一用此日，
                 彻底杜绝凌晨跑被错标为「未开盘的今天」。
    重试只针对上一轮失败的 code（已落盘的 _upsert_quote 会秒跳过，双重保险）；
    重试耗尽后剩下的不管（读路径会显示最近一条历史快照）。

    后半段（分红/情绪）全部带超时，避免 baostock 坏代理卡死整次 --fetch。
    """
    import time as _t

    from stockfu.config import get_fetch_retry_count, get_fetch_retry_interval
    from stockfu.services.quote_writer import validate_ingest_date

    init_db()
    td = validate_ingest_date(target_date)   # 唯一日期权威：非法即报错
    t_all = _t.time()
    # 预热 baostock 免费代理池（PE/分红/状态等依赖；直连已黑名单）
    # 行情主路径：A股个股走 baostock 三复权（同步串行）；baostock 代理池在第
    # 4/6 步才主动 warm，避免东财/腾讯等直连源被代理环境污染
    codes = _current_watch_codes()
    targets = list(dict.fromkeys(codes + INDEX_ETFS))

    print(f"=== [fetch] 1/6 quotes targets={len(targets)} as_of={td} ===", flush=True)
    ok, fail = _batch_fetch_today(targets, td)
    retries = get_fetch_retry_count()
    # 失败重试间隔上限 30s，避免配置成「分钟」拖死
    retry_sleep = min(30, max(1, int(get_fetch_retry_interval()) * 5))
    for i in range(retries):
        if not fail:
            break
        print(f"  retry {i + 1}/{retries} fail={len(fail)} sleep={retry_sleep}s", flush=True)
        if not all(c.startswith(("HK", "US", "au")) for c in fail):
            _t.sleep(retry_sleep)
        ok2, fail = _batch_fetch_today(fail, td)
        ok.extend(ok2)
    print(f"  quotes ok={len(ok)} fail={len(fail)}", flush=True)

    print("=== [fetch] 1b/6 current index universe ===", flush=True)
    try:
        universe_quotes = fetch_universe_quotes(td)
    except Exception as exc:  # noqa: BLE001
        universe_quotes = {
            "total": 0, "pending": 0, "skipped": 0, "ok": 0, "fail": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }
        print(f"  [warn] fetch-universe failed: {universe_quotes['error']}", flush=True)

    print("=== [fetch] 2/6 index + sector ETF ===", flush=True)
    for _idx in ("sh000001", "sz399006", "sh000688"):
        try:
            update_index_benchmark(_idx, td)
        except Exception:  # noqa: BLE001
            pass
    from stockfu.services.composite import SECTOR_MAP as _SECTOR_ETF_MAP
    for _etf in _SECTOR_ETF_MAP.values():
        try:
            update_etf_benchmark(_etf, td)
        except Exception:  # noqa: BLE001
            pass

    print("=== [fetch] 3/6 sector pulse ===", flush=True)
    from stockfu.services import backfill as bf
    sector_pulse = _call_timeout(
        lambda: bf.refresh_sector_pulse_today(td), 300, "sector_pulse", default={},
    ) or {}

    # 后半段：分红 / ETF 份额 / 三层指数
    from stockfu.services import composite, dividend as div_svc
    all_codes = list(codes)

    print("=== [fetch] 4/6 warm baostock proxy (for div/PE) ===", flush=True)
    try:
        from stockfu.data.baostock_proxy import warm_baostock_channel
        warm_baostock_channel()
    except Exception as _e:  # noqa: BLE001
        print(f"  [warn] baostock proxy warm fail: {_e}", flush=True)

    print(f"=== [fetch] 5/6 dividends codes={len(all_codes)} ===", flush=True)
    # 单票 12s；总预算 90s。baostock 分红按年串行，坏代理时最易卡死
    divs = 0
    div_budget = 90.0
    div_t0 = _t.time()
    for i, c in enumerate(all_codes):
        if _t.time() - div_t0 > div_budget:
            print(
                f"  dividends budget {div_budget:.0f}s exhausted "
                f"at {i}/{len(all_codes)}",
                flush=True,
            )
            break
        n = _call_timeout(
            lambda code=c: div_svc.persist_dividends(code),
            12,
            f"div:{c}",
            default=0,
        )
        divs += int(n or 0)
        if (i + 1) % 10 == 0:
            print(f"  dividends {i + 1}/{len(all_codes)} rows+={divs}", flush=True)

    print("=== [fetch] 5b/6 fundflow ETFs ===", flush=True)
    flows = 0
    for c in INDEX_ETFS:
        if _call_timeout(lambda code=c: _upsert_fundflow(code, td), 15, f"flow:{c}", default=False):
            flows += 1

    print(f"=== [fetch] 6/6 composite stocks={len(all_codes)} as_of={td} ===", flush=True)
    # 三层情绪：90s 超时后降级市场+板块（全部用 td 盖章 + as_of 读窗）
    comp = _call_timeout(
        lambda: composite.compute_all(all_codes, td),
        90,
        "compute_all",
        default=None,
    )
    if not isinstance(comp, dict):
        print("  compute_all 超时/失败，降级只算市场+板块情绪", flush=True)
        comp = {}
        try:
            comp["market"] = composite.compute_market(td)
            composite.save(comp["market"], td)
        except Exception as e:  # noqa: BLE001
            print(f"  compute_market err: {e}", flush=True)
        for _name, _etf in composite.SECTOR_MAP.items():
            try:
                _r = _call_timeout(
                    lambda e=_etf, n=_name: composite.compute_sector(e, n, td),
                    20,
                    f"sector:{_name}",
                    default=None,
                )
                if _r and (_r.get("fear") or _r.get("greed") or _r.get("heat")):
                    comp[f"sector:{_name}"] = _r
                    composite.save(_r, td)
            except Exception:  # noqa: BLE001
                pass

    # 自动发信只校验三个市场指数同日：邮件已不渲染个股持仓页，个股抓取仍在
    # 长尾重试（如退市老股）不应阻塞发信。web 手动导出仍走 include_watch=True。
    from stockfu.services.share import export_readiness
    export_data = export_readiness(td, include_watch=False)
    if not export_data["ok"]:
        print(f"  [export blocked] stale={export_data['stale'][:8]}", flush=True)

    summary = {
        "quotes": len(ok),
        "universe_quotes": universe_quotes,
        "retries": retries,
        "still_failed": len(fail),
        "still_failed_codes": fail[:20],
        "dividends": divs,
        "fundflow_etfs": flows,
        "sector_pulse": sector_pulse,
        "composite_levels": len(comp) if isinstance(comp, dict) else 0,
        "export_ready": export_data["ok"],
        "export_stale": export_data["stale"],
        "elapsed_sec": round(_t.time() - t_all, 1),
    }
    print(f"=== [fetch] done {summary} ===", flush=True)
    return summary


def run_mail_fetch(target_date) -> dict:
    """刷新邮件分享卡片实际依赖的市场、行业数据，不触及个股。

    手工 ``--fetch`` 仍走 ``run_scheduled_fetch``，用于更新组合、分红和个股情绪。
    定时邮件则只需要三大指数、行业当日行情/资金流及市场/板块情绪；绝不能因为
    自选股的 baostock 抓取或分红补数失败而拖慢、阻断日报。
    """
    import time as _t

    from stockfu.services.quote_writer import validate_ingest_date

    init_db()
    td = validate_ingest_date(target_date)
    started = _t.time()

    print(f"=== [mail-fetch] 1/3 indices as_of={td} ===", flush=True)
    index_updates: dict[str, int] = {}
    for code in ("sh000001", "sz399006", "sh000688"):
        try:
            index_updates[code] = update_index_benchmark(code, td)
        except Exception as exc:  # noqa: BLE001
            print(f"  [err] index:{code}: {type(exc).__name__}: {exc}", flush=True)
            index_updates[code] = -1

    print("=== [mail-fetch] 2/3 sector pulse ===", flush=True)
    from stockfu.services import backfill as bf
    sector_pulse = _call_timeout(
        lambda: bf.refresh_sector_pulse_today(td), 300, "sector_pulse", default={},
    ) or {}

    print("=== [mail-fetch] 3/3 market + sector composite ===", flush=True)
    from stockfu.services import composite
    # 空列表刻意禁止 compute_all 进入 compute_stock；仍保留市场和板块情绪。
    comp = _call_timeout(
        lambda: composite.compute_all([], td), 90, "compute_mail_composite", default=None,
    )

    from stockfu.services.share import export_readiness
    export_data = export_readiness(td, include_watch=False)
    if not export_data["ok"]:
        print(f"  [export blocked] stale={export_data['stale'][:8]}", flush=True)

    summary = {
        "index_updates": index_updates,
        "sector_pulse": sector_pulse,
        "composite_levels": len(comp) if isinstance(comp, dict) else 0,
        "export_ready": export_data["ok"],
        "export_stale": export_data["stale"],
        "elapsed_sec": round(_t.time() - started, 1),
    }
    print(f"=== [mail-fetch] done {summary} ===", flush=True)
    return summary


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

    from stockfu.config import (
        get_daily_fetch_time,
        get_mail_days,
        get_mail_enabled,
        get_v2_signal_mail_enabled,
        get_v2_signal_mail_time,
        is_mail_ready,
    )

    init_db()
    sched = BlockingScheduler(timezone="Asia/Shanghai")

    # 内嵌 web（mail job 渲染分享卡片时需要本进程的页面）

    if (get_mail_enabled() or get_v2_signal_mail_enabled()) and is_mail_ready():
        start_embedded_server()
        print("✓ 内嵌 web 已启动（供 playwright 渲染分享卡片）")
    else:
        print("· 邮件未启用或未配置完整，跳过内嵌 web（面板配置后重启 --schedule 生效）")

    from stockfu.services.mail import run_mail_job

    def _fetch_then_mail() -> dict:
        """刷新邮件需要的市场/行业数据后自动发邮件（不等定时）。

        不调用全量个股抓取、分红或个股情绪；这些保留给手工 ``--fetch``。守护进程
        取已收盘的最近交易日并过 validate_ingest_date，避免裸 date.today() 错标入库。
        """
        from stockfu.services.quote_writer import (
            latest_closed_trade_day, validate_ingest_date,
        )
        try:
            td = validate_ingest_date(latest_closed_trade_day())
        except ValueError as _e:
            print(f"  [skip fetch] 目标日非法，跳过本次调度: {_e}", flush=True)
            return {"skipped": str(_e)}
        result = run_mail_fetch(td)
        # 分享数据不是同一交易日则不发，避免把混合日期卡片当作当日日报。
        if get_mail_enabled() and is_mail_ready() and result.get("export_ready"):
            try:
                mail_result = run_mail_job()
                result["mail"] = mail_result
            except Exception as exc:  # noqa: BLE001
                result["mail"] = {"ok": False, "detail": str(exc)}
        elif get_mail_enabled() and is_mail_ready():
            result["mail"] = {"ok": False, "detail": "分享数据日期不完整，已跳过发信"}
        if "mail" in result:
            # 2026-09-02 审查:行情卡发信结果此前不落日志,失败时无迹可查
            print(f"[mail] 行情卡发信结果: {result['mail']}", flush=True)
        return result

    hhmm = get_daily_fetch_time()
    h, m = (int(x) for x in hhmm.split(":"))
    sched.add_job(
        _fetch_then_mail,
        CronTrigger(hour=h, minute=m, day_of_week="mon-fri", timezone="Asia/Shanghai"),
        id="daily", max_instances=1, coalesce=True,
        # apscheduler 默认 misfire_grace_time=1s,调度晚醒 >1s 即静默跳过当日任务
        misfire_grace_time=600,
    )
    print(f"✓ 抓取任务：工作日 {hhmm}（北京）抓行情 + 算指数 → 自动发邮件")

    def _run_v2_signal_mail() -> dict:
        """V2 五套策略评分 → 出图 → 发信（as_of=None 内部取最近已收盘交易日）。"""
        from stockfu.config import get_v2_signal_mail_enabled
        from stockfu.services.signal_mail_v2 import run_v2_signal_mail_job

        if not get_v2_signal_mail_enabled():
            print("[v2-mail] 跳过:V2 评分邮件未启用", flush=True)
            return {"skipped": "V2 评分邮件未启用"}
        try:
            # SMTP 未就绪时任务内部自行降级为只出图并返回原因，不抛异常。
            result = run_v2_signal_mail_job(None, send=True)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "detail": f"V2 评分邮件失败: {type(exc).__name__}: {exc}"}
        # 2026-09-02 审查:该链路此前成功/失败均零输出,17:30 静默丢信后无从定位
        print(f"[v2-mail] 结果: {result}", flush=True)
        return result

    signal_hhmm = get_v2_signal_mail_time()
    signal_h, signal_m = (int(value) for value in signal_hhmm.split(":"))
    sched.add_job(
        _run_v2_signal_mail,
        CronTrigger(
            hour=signal_h,
            minute=signal_m,
            day_of_week=get_mail_days(),
            timezone="Asia/Shanghai",
        ),
        id="daily-v2-signal-mail", max_instances=1, coalesce=True,
        misfire_grace_time=600,
    )
    print(
        f"✓ V2 策略评分任务：{get_mail_days()} {signal_hhmm}（北京）"
        "五套正式策略评分 → 出图 → 推荐邮件"
    )

    print("调度已启动，Ctrl-C 退出。")
    sched.start()


def backfill_lhb(*, start: str | None = None, end: str | None = None,
                 refresh: bool = False) -> dict:
    """回补龙虎榜事件(akshare stock_lhb_detail_em,逐日)→ lhb_event。

    PIT:榜单盘后披露,lhb_date 当日可见、T+1 可交易;upsert_lhb_event 硬保证
    cap_date(当日 max)。checkpoint item_key=交易日字符串,断点续传;非交易日
    (空榜)也标成功,避免重复请求。默认 2013-01-01 → 今日;东财直连免代理。
    返回 {"days": 处理天数, "events": 新增事件数, "skipped": 断点跳过天数,
           "failed": 失败天数, "errors": {date: err}}。
    """
    from stockfu.data.akshare_source import get_lhb_daily
    from stockfu.services.backfill_checkpoint import mark_item, pending_items
    from stockfu.services.lhb_writer import upsert_lhb_event

    # 榜单盘后披露:终点/默认截到已收盘最近交易日(盘中本就拿不到当日榜,审查 M2)
    from stockfu.services.quote_writer import latest_closed_trade_day

    cap = latest_closed_trade_day()
    start_d = date.fromisoformat(start) if start else date(2013, 1, 1)
    end_d = min(date.fromisoformat(end) if end else cap, cap)
    days: list[str] = []
    d = start_d
    while d <= end_d:
        days.append(d.isoformat())
        d += timedelta(days=1)
    pending, skipped = pending_items("lhb_event", "v1:daily", days, refresh=refresh)
    print(f"lhb_event checkpoint 跳过:{skipped};待补:{len(pending)};refresh={refresh}",
          flush=True)
    summary = {"days": len(pending), "events": 0, "skipped": skipped,
               "failed": 0, "errors": {}}
    for day_s in pending:
        day = date.fromisoformat(day_s)
        try:
            rows = get_lhb_daily(day)
            if rows:
                cap = max(r["lhb_date"] for r in rows)
                with session_scope() as s:
                    n = upsert_lhb_event(s, rows, cap_date=cap, overwrite=False)
                    s.commit()
                summary["events"] += n
            mark_item("lhb_event", "v1:daily", day_s, success=True)
        except Exception as exc:  # noqa: BLE001
            mark_item("lhb_event", "v1:daily", day_s, success=False,
                      error=f"{type(exc).__name__}: {exc}")
            summary["failed"] += 1
            summary["errors"][day_s] = f"{type(exc).__name__}: {exc}"
        if summary["days"] and len(pending) % 200 == 0:
            print(f"  lhb 进度 {len(pending)}/{summary['days']} 日 "
                  f"(事件 {summary['events']})", flush=True)
    return summary
