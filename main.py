"""StockFu · 资产管理终端 — 统一入口。

用法:
    python main.py                 # 启动 Web（FastAPI + 前端，默认 127.0.0.1:8787）
    python main.py --serve         # 同上
    python main.py --init-db       # 初始化 + 种子自选 + 演示持仓
    python main.py --buy CODE N PRICE [--date YYYY-MM-DD]   # 买入
    python main.py --sell CODE N PRICE [--date]             # 卖出
    python main.py --holdings      # 查看持仓
    python main.py --reset         # 清空持仓和交易
    python main.py --backfill [N]  # 回填 K 线 N 日（默认90；情绪因子建议1825=5年）
    python main.py --backfill-factors    # 回补 两融总量历史 + 个股两融近10天 + 股息率历史序列
    python main.py --backfill-limit [N]  # 回补 连板/涨停历史（默认365天，限速1次/秒+断点续传，慢，建议后台跑）
    python main.py --fetch --date YYYY-MM-DD  # 抓取截至该交易日行情/分红/情绪(必带--date;凌晨防误判)
    python main.py --vacuum        # VACUUM 重建主库(回收空闲页,先备份;停 daemon/回测时跑)
    python main.py --schedule      # 每日定时调度
    python main.py --export-csv [DIR]  # 导出市场数据为 CSV（默认 data/，可入 git）
    python main.py --import-csv [DIR]  # 从 CSV 合并导入回库（换机同步；upsert 不丢数据）
    python main.py --backtest STRATEGY [--start --end --cash --codes --save]  # 回测（见 docs/BACKTEST.md）
    python main.py --backtest-v2-segments ALPHA_ID|all [--codes --snapshot]  # V2 正式三段回测
    python main.py --update-backtests [--strategies a,b] [--start --end] [--dry-run] [--list-strategies]
        # 全周期重跑更新到最新(固化验收口径;不选策略=目录全部)
    python main.py --factor-diag OPERATOR [--start --end --codes --periods --quantiles --params --save]  # 因子诊断（见 docs/BACKTEST.md）
    python main.py --recommend --strategies a,b [--as-of] [--cash]  # 空仓重建荐股(次日开盘执行参考)
    python main.py --v2-watchlist-recommend [--as-of] [--top-n]  # V2十策略自选股荐股
    python main.py --scan-signals --date YYYY-MM-DD [--strategies a,b]  # 800只成分每日0–100评分
    python main.py --test-signal-mail  # 发送最近一次策略评分推荐邮件
    python main.py --backfill-universe  # 回补 security_master(list_date/board, baostock)
    python main.py --audit-corporate-actions  # 只读审计公司行为覆盖/重复/异常（正式回测前置）
    python main.py --backfill-quote-status  # 补历史状态 + 最新交易日全量(baostock)
    python main.py --backfill-adj-prices [--start] [--end]   # baostock 串行三复权(默认 Clash SOCKS)
    python main.py --clear-dividend-cache  # 清错误口径 dividend_yield 的 operator_result
"""
import argparse
import json
from datetime import date


def run_api(host: str, port: int, reload: bool) -> None:
    import uvicorn

    if reload:
        # reload 模式必须传导入字符串（不能是 app 对象），否则 uvicorn 报错退出
        uvicorn.run("stockfu.api.server:app", host=host, port=port, reload=True)
    else:
        from stockfu.api.server import app
        uvicorn.run(app, host=host, port=port)


def run_init_db() -> None:
    from stockfu.db import init_db, seed_demo_holdings, seed_samples

    init_db()
    seed_samples()
    demo = seed_demo_holdings()
    # 算子平台种子(operator/strategy 表 + active 指针);幂等,已有库不重复插
    from stockfu.ai.operators.registry import discover_and_register
    discover_and_register()
    from stockfu.ai.operators.seed import seed_operators_and_strategies
    seed_operators_and_strategies()
    print(f"✓ 数据库已初始化；种子自选 + 演示持仓 + 算子平台已写入: {demo}")


def run_trade(side: str, code: str, shares: str, price: str, d: str | None) -> None:
    from datetime import datetime

    from stockfu.services.trading import add_transaction

    trade_date = datetime.strptime(d, "%Y-%m-%d").date() if d else None
    r = add_transaction(code, side, float(shares), float(price), trade_date)
    verb = "买入" if side == "buy" else "卖出"
    print(f"✓ {verb} {code} {shares}股 @ {price}"
          f"  →  持仓 {r['shares']}股  成本 {r['avg_cost']}  总成本 {r['total_cost']}")


def run_reset() -> None:
    from stockfu.services.trading import reset_all

    reset_all()
    print("✓ 已清空全部交易和持仓（asset 自选保留）")


def run_holdings() -> None:
    from stockfu.services.trading import list_holdings

    rows = list_holdings()
    if not rows:
        print("（无持仓）"); return
    print(f"{'代码':9} {'持仓':>8} {'成本':>10} {'总成本':>12} {'建仓日':12}")
    for r in rows:
        print(f"{r['code']:9} {r['shares']:>8g} {r['avg_cost']:>10.4f} "
              f"{r['total_cost']:>12.2f} {str(r['first_buy'] or ''):12}")


def run_fetch(date_str: str | None) -> None:
    """--fetch: 抓取截至 date_str 的行情/分红/情绪，落库统一用该交易日盖章。

    date_str 必填(YYYY-MM-DD)。非法(未传/未来日/当日未收盘/非交易日)→ 报错退出，
    绝不按裸 date.today() 入库（凌晨跑会被错标为未开盘的今天）。
    """
    import sys

    if not date_str:
        print("✗ --fetch 必须带 --date YYYY-MM-DD（如 --date 2026-07-22）。"
              "不接受裸'今天'：凌晨跑会误判为未开盘日。", file=sys.stderr)
        sys.exit(2)
    from stockfu.services.quote_writer import validate_ingest_date

    try:
        td = validate_ingest_date(date_str)
    except ValueError as e:
        print(f"✗ 拒绝入库：{e}", file=sys.stderr)
        sys.exit(2)
    from stockfu.scheduler.jobs import run_scheduled_fetch

    print(f"✓ 抓取完成（目标交易日 {td}）: {run_scheduled_fetch(td)}")


def run_backfill(days: int) -> None:
    from stockfu.scheduler.jobs import run_backfill

    print(f"回填关键标的 {days} 日历史中…（宽基/行业ETF + 自选）")
    print(f"✓ 回填完成: {run_backfill(days)}")


def run_backfill_factors() -> None:
    from sqlmodel import select

    from stockfu.db import session_scope
    from stockfu.models import Asset
    from stockfu.services import backfill as bf

    with session_scope() as s:
        codes = [a.code for a in s.exec(select(Asset).where(Asset.market == "cn")).all()]
    print(f"补两融总量历史序列: {bf.backfill_margin_total()} 条")
    print(f"补个股两融近10天: {bf.backfill_margin_stock_recent(codes, 10)}")
    dy = {c: bf.compute_dividend_yield_series(c) for c in codes}
    print(f"补股息率历史序列: {dy}")


def run_backfill_benchmark() -> None:
    from stockfu.scheduler.jobs import run_backfill_benchmark as _run

    print("回补回测基准 sh000001 历史日线…")
    print(f"✓ {_run()}")

def run_backfill_sw(*, refresh: bool = False) -> None:
    from stockfu.scheduler.jobs import backfill_sw_index as _run

    print("回补 31 个申万一级行业指数历史日线（akshare index_hist_sw）…")
    print(f"✓ {_run(refresh=refresh)}")


def run_backfill_sector_pulse() -> None:
    from stockfu.services.backfill import backfill_sector_pulse_history

    print("回补同花顺 90 行业历史日线（2020 至今；逐年串行、每请求至少等待 0.3 秒）…")
    print(f"✓ {backfill_sector_pulse_history(pause_sec=0.3)}")

def run_backfill_etf_industry(*, refresh: bool = False) -> None:
    from stockfu.scheduler.jobs import backfill_industry_etf as _run

    print("回补行业 ETF 历史日线（前复权 qfq：东财→腾讯）…")
    print(f"✓ {_run(refresh=refresh)}")


def run_backfill_etf(*, refresh: bool = False, clear: bool = False) -> None:
    """可恢复回补 ETF 前复权日线；仅显式 clear 时清表。"""
    from stockfu.scheduler.jobs import backfill_etf_quotes, clear_etf_data, etf_universe_codes

    codes = etf_universe_codes()
    if clear:
        print("清空 ETF 相关表…")
        cleared = clear_etf_data()
        print(f"  cleared: {cleared}")
    print(f"全量回补 {len(codes)} 只 ETF 前复权日线（东财 qfq→腾讯 qfq）…")
    summary = backfill_etf_quotes(codes, refresh=refresh)
    ok = sum(1 for v in summary.values() if v.get("total", 0) > 0)
    deep = sum(1 for v in summary.values() if v.get("source_hint") == "em_or_deep")
    empty = [c for c, v in summary.items() if not v.get("total")]
    print(f"✓ 成功 {ok}/{len(codes)}  东财深历史≈{deep}  空={empty or '无'}")

def run_backfill_limit(days: int) -> None:
    from stockfu.services import backfill as bf

    print(f"回补连板/涨停 {days} 天（限速1次/秒 + 断点续传，慢，建议后台跑）…")
    print(f"✓ {bf.backfill_limit_up(days)}")


def run_schedule() -> None:
    from stockfu.scheduler.jobs import run_schedule as _run

    _run()


def run_clean_quotes() -> None:
    from stockfu.scheduler.jobs import clean_quote_snapshots

    print(f"✓ 清理非交易日快照: {clean_quote_snapshots()}")


def run_vacuum() -> None:
    """VACUUM INTO 新文件 + 原子替换(先备份)。停 daemon/回测时跑。

    G09 维护工具:删冗余索引 / cleanup_operator_results 全表扫 DELETE 后会留空闲页,
    跑此回收空间。freelist=0 时无瘦身(纯整理)。VACUUM 不能在事务内跑→AUTOCOMMIT;
    VACUUM INTO 产出含已提交 WAL 数据的独立新库,os.replace 原子换主库后删旧 -wal/-shm,
    下次连接由 connect 监听器自动恢复 WAL 模式并重建旁路文件。
    """
    import os
    import shutil
    from pathlib import Path

    from stockfu.config import DATA_DIR
    from stockfu.db import engine

    db = DATA_DIR / "stockfu.db"
    bak = DATA_DIR / "stockfu.db.bak.G09"
    vac = DATA_DIR / "stockfu.db.vac"
    if not db.exists():
        print(f"✗ 库不存在: {db}")
        return
    print(f"备份 {db} → {bak} …")
    shutil.copy2(db, bak)
    if vac.exists():
        vac.unlink()
    before = db.stat().st_size
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.exec_driver_sql(f"VACUUM INTO '{vac}'")
    engine.dispose()                       # 释放指向旧 inode 的池连接,否则 os.replace 后写丢失
    os.replace(vac, db)                    # 原子替换
    for suf in ("-wal", "-shm"):           # 旧旁路失效(VACUUM INTO 已含其数据),删之,首连重建
        p = Path(str(db) + suf)
        if p.exists():
            p.unlink()
    after = db.stat().st_size
    print(f"✓ VACUUM 完成: {before / 1024 / 1024:.1f}MB → {after / 1024 / 1024:.1f}MB;备份 {bak}")
    print("  (下次连接自动重建 -wal/-shm 并恢复 WAL 模式)")


def run_test_mail() -> None:
    import time

    from stockfu.scheduler.jobs import start_embedded_server
    from stockfu.services.mail import run_mail_job

    start_embedded_server()          # 内嵌 web：--test-mail 自包含，无需另开 --serve
    time.sleep(2.5)                  # 等 serve 就绪再渲染
    print(f"✓ 邮件任务结果: {run_mail_job()}")


def run_signal_scan_cli(date_str: str | None, strategies: str | None) -> None:
    import sys

    if not date_str:
        print("✗ --scan-signals 必须带 --date YYYY-MM-DD", file=sys.stderr)
        raise SystemExit(2)
    from stockfu.services.quote_writer import validate_ingest_date
    try:
        signal_date = validate_ingest_date(date_str)
    except ValueError as exc:
        print(f"✗ 拒绝扫描：{exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    strategy_ids = [value.strip() for value in (strategies or "").split(",") if value.strip()] or None
    from stockfu.scheduler.jobs import run_signal_pipeline
    print(f"✓ 策略评分完成: {run_signal_pipeline(signal_date, strategy_ids=strategy_ids)}")


def run_test_signal_mail() -> None:
    import time

    from stockfu.scheduler.jobs import start_embedded_server
    from stockfu.services.signal_mail import run_signal_mail_job

    start_embedded_server()
    time.sleep(2.5)
    print(f"✓ 推荐邮件任务结果: {run_signal_mail_job(force=True)}")


def run_config() -> None:
    from stockfu.wizard import run_wizard

    run_wizard()


def run_backfill_universe() -> None:
    """回补 security_master(list_date / delist / board),宇宙层前置。"""
    from stockfu.db import init_db
    from stockfu.services.universe import backfill_security_master

    init_db()
    print("回补 security_master(baostock query_stock_basic) …")
    r = backfill_security_master()
    print(f"✓ upserted={r.get('upserted')}  baostock={r.get('from_baostock')}  "
          f"first_quote兜底={r.get('from_first_quote')}  skipped={r.get('skipped')}")
    if r.get("error"):
        print(f"  警告: {r['error']}")


def run_audit_corporate_actions(start_year: int, end_year: int | None) -> None:
    """输出正式回测前的公司行为覆盖与冲突报告；不联网、不写库。"""
    import json

    from stockfu.services.dividend import audit_corporate_actions

    print(json.dumps(
        audit_corporate_actions(start_year=start_year, end_year=end_year),
        ensure_ascii=False, indent=2, sort_keys=True,
    ))


def run_repair_known_dividend_conflicts() -> None:
    """应用已审计的分红冲突裁决，随后可只重试失败 checkpoint。"""
    from stockfu.db import init_db
    from stockfu.services.dividend import repair_known_dividend_conflicts

    init_db()
    print("应用已审计的分红冲突裁决（13 项）…")
    print(f"✓ {repair_known_dividend_conflicts()}")


def run_backfill_index_universe(index_codes: str | None) -> None:
    """导入带有效日期的中证当前快照；历史档案须逐期补齐，绝不倒灌。"""
    from stockfu.db import init_db
    from stockfu.services.index_universe import (
        HISTORICAL_INDEX_CODES, audit_coverage, fetch_official_current_snapshot,
        normalize_index_codes,
    )

    init_db()
    codes = normalize_index_codes(index_codes.split(",") if index_codes else HISTORICAL_INDEX_CODES)
    for code in codes:
        try:
            print(fetch_official_current_snapshot(code))
        except Exception as exc:  # noqa: BLE001
            print({"index_code": code, "error": f"{type(exc).__name__}: {exc}"})
    print(audit_coverage(codes))


def run_backfill_index_universe_history(start: str, end: str, *, refresh: bool = False) -> None:
    """从 BaoStock 可复现历史接口回补默认 300+500；仍待正式档案核验。"""
    from datetime import date
    from stockfu.db import init_db
    from stockfu.services.index_universe import (
        HISTORICAL_INDEX_CODES, audit_coverage, backfill_baostock_historical_indices,
    )

    init_db()
    start_d, end_d = date.fromisoformat(start), date.fromisoformat(end)
    if end_d < start_d:
        raise ValueError("--index-history-end 不能早于 --index-history-start")
    print(f"回补沪深300+中证500历史快照（baostock，串行、待正式档案核验）: {start_d} → {end_d}")
    result = backfill_baostock_historical_indices(start=start_d, end=end_d, refresh=refresh)
    print(result)
    print(audit_coverage(HISTORICAL_INDEX_CODES))


def run_backfill_index_universe_mirror(start: str, end: str) -> None:
    """导入可追溯月度镜像；它只补覆盖，不能作为日级正式历史。"""
    from datetime import date
    from stockfu.db import init_db
    from stockfu.services.index_universe import audit_coverage, backfill_yfiua_csi1000

    init_db()
    start_d, end_d = date.fromisoformat(start), date.fromisoformat(end)
    if end_d < start_d:
        raise ValueError("--index-history-end 不能早于 --index-history-start")
    print(f"导入中证1000月度镜像（待正式档案核验）: {start_d} → {end_d}")
    print(backfill_yfiua_csi1000(start=start_d, end=end_d))
    print(audit_coverage(("000852",)))


def run_backfill_star50_initial() -> None:
    from stockfu.db import init_db
    from stockfu.services.index_universe import audit_coverage, import_sse_star50_initial_snapshot

    init_db()
    print(import_sse_star50_initial_snapshot())
    print(audit_coverage(("000688",)))


def run_backfill_quote_status(*, refresh: bool = False) -> None:
    """补全:历史 is_st/trade_status + 每只票最新交易日全量数据(OHLCV/估值/状态)。"""
    from stockfu.db import init_db
    from stockfu.scheduler.jobs import backfill_quote_status
    from stockfu.services.universe import quote_status_coverage

    init_db()
    print("补全 quote_snapshot(历史状态 + 最新交易日全量, baostock) …")
    before = quote_status_coverage()
    print(f"  前: is_st_rate={before.get('is_st_rate')}  "
          f"trade_status_rate={before.get('trade_status_rate')}  rows={before.get('n_rows')}")
    r = backfill_quote_status(refresh=refresh)
    after = quote_status_coverage()
    print(f"✓ codes={r.get('codes')}  历史状态补丁={r.get('rows_patched')}  "
          f"最新日全量upsert={r.get('latest_upserted')}  errors={r.get('errors')}")
    print(f"  最新交易日区间: {r.get('latest_date_min')} ~ {r.get('latest_date_max')}")
    print(f"  后: is_st_rate={after.get('is_st_rate')}  "
          f"trade_status_rate={after.get('trade_status_rate')}")


def run_backfill_adj_prices(start: str | None = None, end: str | None = None,
                            no_socks: bool = False,
                            proxy_mode: str | None = None,
                            full: bool = False) -> None:
    """baostock **串行** 三复权写入 quote_snapshot.*_qfq/*_raw/*_hfq。

    默认 proxy_mode=free：启动拉免费代理入池 + 本机 Clash 种子；
    单 IP 串行，失败立即剔除并切换。baostock 裸 TCP 经 CONNECT/SOCKS 隧道。
    """
    from stockfu.db import init_db
    from stockfu.scheduler.backfill_adj_prices import (
        adj_price_coverage, backfill_adj_prices, clear_dividend_yield_cache,
    )

    init_db()
    mode = (proxy_mode or "free").strip().lower()
    if no_socks:
        mode = "direct"
    before = adj_price_coverage()
    print(f"回补前覆盖: rows={before['rows']} qfq={before['has_qfq']} "
          f"raw={before['has_raw']}({before['raw_pct']}%) "
          f"hfq={before['has_hfq']}({before['hfq_pct']}%)")
    print(f"代理模式: {mode}")
    r = backfill_adj_prices(
        start=start or "2020-01-01",
        end=end,
        proxy_mode=mode,  # type: ignore[arg-type]
        preserve_qfq=True,
        resume=not full,
    )
    after = adj_price_coverage()
    print(f"回补后覆盖: rows={after['rows']} qfq={after['has_qfq']} "
          f"raw={after['has_raw']}({after['raw_pct']}%) "
          f"hfq={after['has_hfq']}({after['hfq_pct']}%)")
    print(f"  rotates={r.get('rotates')} dropped={r.get('dropped')} "
          f"proxy={r.get('proxy')}")
    if r.get("error_n"):
        print(f"  失败样例: {r['errors'][:5]}")
    n = clear_dividend_yield_cache()
    print(f"✓ 已清 dividend_yield 缓存 {n} 行(新口径 price_basis=raw)")


def run_clear_dividend_cache() -> None:
    from stockfu.db import init_db
    from stockfu.scheduler.backfill_adj_prices import clear_dividend_yield_cache

    init_db()
    n = clear_dividend_yield_cache()
    print(f"✓ 已删除 operator_result dividend_yield {n} 行")


def run_backfill_dividend(start_year: int | None = None, *, refresh: bool = False) -> None:
    """回补全市场 A 股分红历史 → dividend_event 表。

    baostock query_dividend_data 主源(财年口径,默认 2007 至今以填早期空白),akshare 兜底。
    resolve_base_codes('all') 取 quote_snapshot 全池(~800 票);按 ex_date 去重,
    幂等可重跑。baostock socket 轻量单线程,预计 10-20 分钟,建议后台跑。
    """
    from stockfu.db import init_db, session_scope
    from sqlalchemy import text
    from datetime import date
    from stockfu.services.universe import resolve_base_codes
    from stockfu.services import dividend as div_svc

    init_db()
    start_year = start_year or 2007
    if start_year < 1990 or start_year > date.today().year:
        raise ValueError(f"非法分红回补起始年: {start_year}")
    years = date.today().year - start_year + 1
    codes = resolve_base_codes("all")
    scope = f"v1:{start_year}-{date.today().year}"
    from stockfu.services.backfill_checkpoint import mark_item, pending_items
    pending, skipped = pending_items("dividend", scope, codes, refresh=refresh)
    with session_scope() as s:
        before = s.exec(text("SELECT COUNT(*) FROM dividend_event")).all()[0][0]
    print(f"回补 {len(codes)} 只 A 股分红历史({start_year}→{date.today().year}; "
          f"baostock 主源 / akshare 兜底;前:{before} 行; "
          f"checkpoint 跳过:{skipped};待补:{len(pending)};refresh={refresh})…")
    new = errors = 0
    for i, c in enumerate(pending, 1):
        try:
            # 历史请求每只会查多个财年，给 baostock 更长的线程看门狗时间。
            new += div_svc.persist_dividends(c, years=years, timeout=60.0)
            mark_item("dividend", scope, c, success=True)
        except Exception as e:  # noqa: BLE001
            errors += 1
            mark_item("dividend", scope, c, success=False,
                      error=f"{type(e).__name__}: {e}")
            if errors <= 5:
                print(f"  ⚠ {c} 失败: {e}")
        if i % 50 == 0 or i == len(pending):
            print(f"  [{i}/{len(pending)}] 累计新增 {new} 条 (失败 {errors})")
    with session_scope() as s:
        after = s.exec(text("SELECT COUNT(*) FROM dividend_event")).all()[0][0]
    print(f"✓ 完成:新增 {new} 条,失败 {errors} 只;dividend_event {before} → {after} 行")


def run_update_backtests(
    strategies: str | None,
    start: str | None,
    end: str | None,
    cash: float,
    dry_run: bool,
    list_only: bool,
) -> None:
    """全周期策略回测更新到最新:见 stockfu.backtest.full_cycle_update。

    strategies: 逗号分隔 strategy_id;None/空 = 目录全部。
    """
    from stockfu.backtest.v1_gate import ensure_v1_backtest_enabled
    ensure_v1_backtest_enabled()
    from stockfu.backtest.full_cycle_update import (
        print_catalog,
        update_backtests,
    )

    if list_only:
        print_catalog()
        return
    ids = None
    if strategies and strategies.strip():
        ids = [x.strip() for x in strategies.split(",") if x.strip()]
    try:
        update_backtests(
            ids,
            start=start,
            end=end,
            cash=cash,
            dry_run=dry_run,
            save_summary=True,
        )
    except ValueError as e:
        print(f"✗ {e}")
        raise SystemExit(2) from e


def run_v2_signal_mail(as_of: str | None, no_send: bool, top_n: int) -> None:
    """V2 十策略单日评分 → 出图 → 发信(默认最新交易日;可 --as-of 指定)。"""
    import json
    from datetime import date

    from stockfu.db import init_db
    from stockfu.services.signal_mail_v2 import run_v2_signal_mail_job

    init_db()
    d = date.fromisoformat(as_of) if as_of else None
    res = run_v2_signal_mail_job(d, top_n=top_n, send=not no_send)
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    if not res.get("ok"):
        raise SystemExit(1)


def run_v2_watchlist_recommend(as_of: str | None, top_n: int) -> None:
    """V2 当前十策略在自选股票范围评分，保存完整荐股报告。"""
    from stockfu.db import init_db
    from stockfu.services.v2_recommend import (
        print_v2_watchlist_recommendation,
        run_v2_watchlist_recommendation,
    )
    from stockfu.services.quote_writer import latest_closed_trade_day

    init_db()
    d = as_of or latest_closed_trade_day().isoformat()
    try:
        result = run_v2_watchlist_recommendation(d, save=True)
    except ValueError as exc:
        print(f"✗ {exc}")
        raise SystemExit(2) from exc
    print_v2_watchlist_recommendation(result, top_n=top_n)


def run_recommend(
    strategies: str | None,
    as_of: str | None,
    cash: float,
    slip_bps: float,
    band_pct: float,
    max_gross: float | None,
    min_amount: float | None,
    with_sentiment: bool,
    write_cache: bool,
) -> None:
    """空仓重建荐股:策略+回测 meta → as_of 信号日 → 次日执行参考价/估值中枢。"""
    from stockfu.db import init_db
    from stockfu.services.recommend import (
        available_strategies,
        print_report,
        run_recommend as _run,
    )

    init_db()
    if not strategies or not str(strategies).strip():
        print("✗ --recommend 必须指定 --strategies a,b")
        print(f"  可选: {', '.join(available_strategies())}")
        raise SystemExit(2)
    ids = [x.strip() for x in str(strategies).split(",") if x.strip()]
    try:
        report = _run(
            ids,
            as_of=as_of,
            cash=cash,
            slip_bps=slip_bps,
            band_pct=band_pct,
            max_gross=max_gross,
            min_amount=min_amount,
            with_sentiment=with_sentiment,
            write_cache=write_cache,
            save=True,
        )
    except ValueError as e:
        print(f"✗ {e}")
        raise SystemExit(2) from e
    print_report(report)


def run_watchlist_review(
    strategies: str | None,
    as_of: str | None,
    pool_spec: str,
    codes_override: str | None,
    add: list[str],
    drop: list[str],
    with_sentiment: bool,
    with_llm: bool,
    write_cache: bool,
) -> None:
    """自选股多策略评价矩阵:解耦引擎 evaluate(codes, strategy_ids, as_of)。

    股票池:默认 watchlist;--codes 显式覆盖;--add/--drop 临时增删(不写 DB)。
    策略:--strategies 任意入库 id(默认 active_strategy_id),不限 catalog。
    """
    from stockfu.db import init_db
    from stockfu.services.evaluator import (
        available_strategy_ids, run_watchlist_review as _run,
    )
    init_db()
    sids = [s.strip() for s in (strategies or "").split(",") if s.strip()] or None
    codes_list = (
        [c.strip() for c in codes_override.split(",") if c.strip()]
        if codes_override else None
    )
    try:
        _run(
            pool_spec=pool_spec,
            codes_override=codes_list,
            add=add or None,
            drop=drop or None,
            strategies=sids,
            as_of=as_of,
            with_sentiment=with_sentiment,
            with_llm=with_llm,
            write_cache=write_cache,
            save=True,
        )
    except ValueError as e:
        print(f"✗ {e}")
        if "未知 strategy_id" in str(e) or "可选" in str(e):
            pass  # 错误信息已含可选列表
        else:
            print(f"  可用策略: {', '.join(available_strategy_ids())}")
        raise SystemExit(2) from e


def run_v2_backtest_cli(alpha_id: str, start: str | None, end: str | None,
                        cash: float, codes: str | None,
                        portfolio_id: str | None, risk_id: str | None,
                        history_origin: str | None,
                        observation_count: int | None,
                        checkpoint_path: str | None,
                        resume_from: str | None,
                        checkpoint_every: int,
                        snapshot_path: str | None = None,
                        canonical: bool = False) -> None:
    """V2 回测 CLI:加载 alpha/profile/portfolio/risk,跑 v2_engine,打印绩效。

    --codes 省略=全 A 股候选池(quote_snapshot 区间内有行情的 00/30/60/68)。
    """
    from datetime import date

    # fail-closed 预检（§4.13.3-2）：canonical 门禁必须先于一切副作用——
    # init_db 会写主库 schema，resolve_snapshot 可能创建 GB 级快照。
    from stockfu.backtest.v2_engine import canonical_preflight
    canonical_preflight(canonical)
    from stockfu.backtest.v2_run import (
        default_universe, historical_full_universe,
        historical_full_universe_rules, historical_hs300_universe_rules,
        hs300_universe, run,
        validate_v2_alpha_id,
    )
    # 旧 V1 id 必须在 init_db/快照等副作用之前 fail-closed，明确指向归档映射。
    validate_v2_alpha_id(alpha_id)
    from stockfu.db import init_db
    init_db()
    from stockfu.services.universe import resolve_base_codes

    end_d = end or date.today().isoformat()
    start_d = start or "2021-01-01"
    es = date.fromisoformat(start_d)
    ee = date.fromisoformat(end_d)
    ho = date.fromisoformat(history_origin) if history_origin else None
    # 数据快照在股票池解析前确定（阻塞①）：--snapshot 复用既有快照，否则新建或
    # 从 resume 工件恢复；候选池解析必须在快照只读上下文内，确保其来自快照。
    from stockfu.backtest.snapshot import descriptor_from_file, snapshot_engine
    from stockfu.backtest.v2_engine import resolve_snapshot
    from stockfu.db import use_read_engine
    provided = descriptor_from_file(snapshot_path) if snapshot_path else None
    snap = resolve_snapshot(provided=provided, resume_from=resume_from,
                            snapshots_dir=None)
    with use_read_engine(snapshot_engine(snap)):
        universe_rules = None
        low_codes = codes.lower() if codes else None
        if low_codes == "hs300":
            code_list = hs300_universe()
            universe_rules = historical_hs300_universe_rules()
        elif low_codes in ("historical_indices", "historical_index", "csi300_csi500"):
            code_list = historical_full_universe()
            universe_rules = historical_full_universe_rules()
        elif codes:
            code_list = resolve_base_codes(codes)
        else:
            code_list = default_universe(es, ee)
    print(f"V2 回测 {alpha_id}  {start_d} → {end_d}  {len(code_list)}只票  "
          f"历史→{ho or '默认前5年'}  资金 {cash:,.0f} …")
    res = run(alpha_id, eval_start=es, eval_end=ee, codes=code_list,
              portfolio_id=portfolio_id, risk_id=risk_id, history_origin=ho,
              initial_cash=cash if cash else None,
              observation_count=observation_count, universe_rules=universe_rules,
              checkpoint_path=checkpoint_path, resume_from=resume_from,
              checkpoint_every=checkpoint_every, snapshot=snap,
              canonical=canonical)
    m = res.metrics
    print(f"\n=== V2 绩效(formal {res.formal_summary['n_days']} 日,基准 {res.manifest['benchmark_code']}) ===")
    for k in ("total_return", "annualized", "max_drawdown", "sharpe", "calmar",
              "win_rate", "benchmark_return", "excess", "sortino"):
        if k in m:
            print(f"  {k}: {m[k]}")
    print(f"  首单日 {res.first_trade_date}  末单日 {res.last_trade_date}  成交 {len(res.trades)} 笔")
    print(f"  观察期 raw missing_rate: {res.observation_summary['missing_rate']}")
    risk_metrics = res.manifest.get("risk_metrics") or {}
    if risk_metrics:
        print(f"  风控触发: {risk_metrics}")
    cov = res.manifest.get("data_coverage") or {}
    if cov.get("truncated"):
        print(f"  ⚠ 数据截断: 请求终点 {cov['requested_eval_end']}，库数据只到 "
              f"{cov['data_end']}，实际跑至 {cov['effective_eval_end']}")
    diag = res.score_diagnostics or {}
    if diag:
        s = diag.get("score") or {}
        cov_f = (diag.get("score_coverage") or {}).get("formal") or {}
        print(f"  §15 诊断: score n={s.get('n')} p50={s.get('p50')} "
              f"p01={s.get('p01')} p99={s.get('p99')} "
              f"0/100饱和={s.get('saturation_0_100')}% "
              f"横截面唯一值比={s.get('unique_ratio')}%({s.get('unique_ratio_days')}日) "
              f"formal coverage均值={cov_f.get('mean')} "
              f"clamp={diag.get('factor_clamp_rate')} "
              f"maturity={diag.get('factor_maturity')} "
              f"maturity_delay={diag.get('maturity_delay_days')}天")
    print(f"  run_id: {res.manifest['run_id'][:16]}  formal_start: {res.manifest.get('formal_start')}")


def run_v2_segmented_cli(
    alpha_selection: str,
    start: str | None,
    end: str | None,
    cash: float,
    codes: str | None,
    portfolio_id: str | None,
    risk_id: str | None,
    history_origin: str | None,
    observation_count: int | None,
    checkpoint_path: str | None,
    resume_from: str | None,
    checkpoint_every: int,
    snapshot_path: str | None = None,
    canonical: bool = False,
    segments: str | None = None,
    output_root: str = "data/backtest/v2-segments",
    variant_id: str = "daily",
    resume_suite: str | None = None,
) -> None:
    """V2 正式分段回测：同一部署独立跑 full/2013-2019/2020-2026。"""
    from datetime import date, datetime
    from pathlib import Path

    if start or end:
        raise SystemExit("✗ --backtest-v2-segments 使用固定三段区间，不接受 --start/--end")
    if checkpoint_path or resume_from:
        raise SystemExit(
            "--backtest-v2-segments 自动管理每段 checkpoint，不接受 --checkpoint/--resume"
        )

    from stockfu.backtest.segments import FULL_SEGMENT
    from stockfu.backtest.v2_engine import canonical_preflight, resolve_snapshot
    from stockfu.backtest.v2_run import (
        default_universe, historical_full_universe,
        historical_full_universe_rules, historical_hs300_universe_rules,
        hs300_universe, validate_v2_alpha_id,
    )
    from stockfu.backtest.snapshot import descriptor_from_file, snapshot_engine
    from stockfu.backtest.v2_suite import (
        V2Deployment, resolve_alpha_ids, run_segmented_backtests,
    )
    from stockfu.db import init_db, use_read_engine
    from stockfu.services.universe import resolve_base_codes

    canonical_preflight(canonical)
    alpha_ids = resolve_alpha_ids(alpha_selection)
    for alpha_id in alpha_ids:
        validate_v2_alpha_id(alpha_id)
    init_db()
    if resume_suite:
        suite_manifest_path = Path(resume_suite) / "suite.json"
        if not suite_manifest_path.is_file():
            raise SystemExit(
                f"✗ --resume-segment-suite 缺少 suite.json: {suite_manifest_path}"
            )
        old_suite = json.loads(suite_manifest_path.read_text(encoding="utf-8"))
        provided = (
            descriptor_from_file(snapshot_path) if snapshot_path
            else old_suite.get("snapshot")
        )
        if not provided:
            raise SystemExit(
                "✗ 续跑旧 suite 时无法从 suite.json 找到快照 descriptor；"
                "请显式提供 --snapshot SNAPSHOT.db"
            )
    else:
        provided = descriptor_from_file(snapshot_path) if snapshot_path else None
    snap = resolve_snapshot(provided=provided, resume_from=None, snapshots_dir=None)
    with use_read_engine(snapshot_engine(snap)):
        universe_rules = None
        low_codes = codes.lower() if codes else None
        if low_codes == "hs300":
            code_list = hs300_universe()
            universe_rules = historical_hs300_universe_rules()
        elif low_codes in ("historical_indices", "historical_index", "csi300_csi500"):
            code_list = historical_full_universe()
            universe_rules = historical_full_universe_rules()
        elif codes:
            code_list = resolve_base_codes(codes)
        else:
            code_list = default_universe(FULL_SEGMENT.eval_start, FULL_SEGMENT.eval_end)

    root = Path(output_root)
    run_root = Path(resume_suite) if resume_suite else root / f"run-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}"
    ho = date.fromisoformat(history_origin) if history_origin else None
    deployments = tuple(
        V2Deployment(
            alpha_id=alpha_id,
            variant_id=variant_id,
            portfolio_id=portfolio_id,
            risk_id=risk_id,
        )
        for alpha_id in alpha_ids
    )
    suite = run_segmented_backtests(
        deployments,
        output_root=run_root,
        segments=segments,
        codes=code_list,
        universe_rules=universe_rules,
        history_origin=ho,
        initial_cash=cash if cash else None,
        observation_count=observation_count,
        checkpoint_every=checkpoint_every,
        snapshot=snap,
        canonical=canonical,
        resume_existing=bool(resume_suite),
    )
    print(f"✓ 分段回测完成: {len(suite.runs)} 个区间运行")
    print(f"  suite: {suite.manifest_path}")
    for item in suite.runs:
        metrics = item.summary.get("metrics") or {}
        print(
            f"  {item.deployment.alpha_id} [{item.deployment.variant_id}] "
            f"{item.segment.segment_id}: 总收益 {metrics.get('total_return')}% "
            f"年化 {metrics.get('annualized')}% 回撤 {metrics.get('max_drawdown')}% "
            f"Sharpe {metrics.get('sharpe')}"
        )


def run_backtest(strategy: str, start: str | None, end: str | None,
                 cash: float, codes: str | None, save: bool,
                 min_amount: float | None = None,
                 valuation_basis: str = "qfq") -> None:
    """回测：算子→策略→逐日 T+1 执行，输出绩效指标。

    策略由 app_config('active_strategy_id') 决定;此处 --backtest STRATEGY 设置它。
    --codes: 省略=沪深300+中证500时点成分宇宙；all/pool=大盘候选池；或逗号列表。
    估值口径默认 qfq(研究模式主线,已含分红再投);研究模式详见 docs/BACKTEST.md §0。
    """
    from stockfu.backtest.v1_gate import ensure_v1_backtest_enabled
    ensure_v1_backtest_enabled()
    from datetime import date, timedelta

    from stockfu.db import init_db, set_app_config, session_scope
    init_db()
    # 校验策略在 DB(含变体 base#key),避免 set_app_config 后被 get_active_strategy
    # 静默回落 pure_factor——复合 id 拼写错会无声跑错策略。
    from sqlmodel import select
    from stockfu.models import Strategy
    with session_scope() as s:
        if s.get(Strategy, strategy) is None:
            avail = sorted(r for r in s.exec(select(Strategy.strategy_id)).all())
            raise SystemExit(
                f"策略 '{strategy}' 不在 DB(先 --init-db / seed)。可用: {avail}"
            )
    set_app_config("active_strategy_id", strategy)
    from stockfu.ai.operators.registry import discover_and_register
    discover_and_register()
    from stockfu.backtest.scheduler import run as _run
    from stockfu.services.index_universe import HISTORICAL_INDEX_CODES, HISTORICAL_UNIVERSE_ID
    from stockfu.services.universe import UniverseRules, resolve_base_codes

    end_d = end or date.today().isoformat()
    start_d = start or (date.today() - timedelta(days=365)).isoformat()
    use_historical_universe = codes is None
    code_list = resolve_base_codes("historical_indices" if use_historical_universe else codes)

    scope = f"{len(code_list)}只票"
    print(f"回测 {strategy}  {start_d} → {end_d}  初始资金 {cash:,.0f}  ({scope}, 估值 {valuation_basis}) …")
    universe_rules = None
    if use_historical_universe:
        universe_rules = UniverseRules(
            universe_id=HISTORICAL_UNIVERSE_ID,
            index_codes=HISTORICAL_INDEX_CODES,
            min_amount_ma20=min_amount,
        )
    elif min_amount is not None:
        universe_rules = UniverseRules(min_amount_ma20=min_amount)
    r = _run(code_list, start_d, end_d, initial_cash=cash,
             universe_rules=universe_rules, valuation_basis=valuation_basis)
    m = r["metrics"]
    bench_ret = m.get("benchmark_return")
    window = m.get("benchmark_window")
    if bench_ret is not None:
        win_str = f" 基准窗口 {window['start']}~{window['end']}" if window else ""
        bench_str = f" | 基准 {bench_ret}%{win_str}"
    else:
        reason = m.get("benchmark_reason", "N/A")
        bench_str = f" | 基准 {reason}"
    wr = m.get("win_rate")
    wr_str = f" | 胜率 {wr}%" if wr is not None else ""
    excess = m.get("excess")
    excess_str = f" | 超额 {excess}%" if excess is not None else ""
    _rec = m.get("max_drawdown_recovery_days")
    rec_str = f" | 回本 {_rec}d" if _rec is not None else " | 回本 未回本"
    _slc = m.get("stop_loss_count")
    sl_str = f" | 止损 {_slc}笔" if _slc else ""
    _tov = m.get("avg_daily_turnover")
    tov_str = (f" | 日均换手 {_tov}只/日(年化{m.get('annual_turnover')}遍)"
               if _tov is not None else "")
    uni = m.get("config", {}).get("universe") or r.get("universe") or {}
    uni_str = ""
    if uni.get("avg_size") is not None:
        uni_str = (f"\n  宇宙 {uni.get('universe_id')} 日均 {uni['avg_size']} "
                   f"[{uni.get('min_size')}~{uni.get('max_size')}] "
                   f"master {uni.get('master_coverage')}/{uni.get('base_size')}")
    print(f"✓ 总收益 {m.get('total_return')}% | 年化 {m.get('annualized')}% | "
          f"最大回撤 {m.get('max_drawdown')}% | 夏普 {m.get('sharpe')}{wr_str}{bench_str}{excess_str}{rec_str}{sl_str}\n"
          f"  交易 {m.get('trade_count')}笔{tov_str} | 期末权益 {m.get('final_equity')}"
          f" | 涨停拒买 {m.get('limit_reject_buys', 0)} | 跌停拒卖 {m.get('limit_reject_sells', 0)}"
          f"{uni_str}")
    if r.get("saved_to"):
        print(f"  结果已保存: {r['saved_to']}")


def _parse_periods(spec: str | None) -> tuple[int, ...] | None:
    if not spec:
        return None
    return tuple(int(p.strip()) for p in spec.split(",") if p.strip())


def run_factor_diag(operator: str, start: str | None, end: str | None,
                    codes: str | None, params: str | None,
                    periods: tuple[int, ...] | None, quantiles: int,
                    primary_period: int | None, save: bool) -> None:
    """因子诊断：单算子连续 score 的 IC / 分位收益 / 换手 / 衰减（alphalens 思路）。

    不搭策略管道，直接量化单个因子在全市场横截面上对前向收益的预测力。
    score 走回测算子缓存(operator_result)，与回测互通复用。详见 docs/BACKTEST.md。
    """
    import json
    import os
    from datetime import date, datetime, timedelta

    from stockfu.ai.operators.registry import discover_and_register, get_operator_class
    from stockfu.backtest.factor_diag import (run_factor_diag as _run,
                                              DEFAULT_PERIODS)

    discover_and_register()
    cls = get_operator_class(operator)
    if cls is None:
        from stockfu.ai.operators.registry import REGISTRY
        avail = sorted(k for k, c in REGISTRY.items() if c.type == "math")
        print(f"✗ 未知算子 '{operator}'；可用 math 算子: {', '.join(avail)}")
        return

    end_d = end or date.today().isoformat()
    start_d = start or (date.today() - timedelta(days=365)).isoformat()
    from stockfu.db import init_db
    from stockfu.services.universe import resolve_base_codes
    init_db()
    # all/pool → 大盘候选 ~800;省略 → 自选;每日再按 U(t) 滤次新/ST/停牌
    code_list = resolve_base_codes(codes)

    # 默认参数 = 算子 PARAMS_SCHEMA（各算子的默认窗口/周期）
    if params:
        try:
            op_params = json.loads(params)
        except json.JSONDecodeError as e:
            print(f"✗ --params JSON 解析失败: {e}")
            return
    else:
        op_params = dict(getattr(cls, "PARAMS_SCHEMA", {})) or {}
    periods_t = periods or DEFAULT_PERIODS
    pperiod = primary_period if primary_period is not None else (
        5 if 5 in periods_t else periods_t[len(periods_t) // 2])

    pstr = ", ".join(f"{k}={v}" for k, v in op_params.items()) or "默认"
    print(f"因子诊断  {operator}({pstr})  {start_d} → {end_d}  ({len(code_list)}只票) …")
    rep = _run(operator, op_params, code_list, start_d, end_d,
               periods=periods_t, n_quantiles=quantiles, primary_period=pperiod,
               progress=True)

    um = rep.get("universe") or {}
    print(f"\n标的池 base {rep['universe_size']} 只 | 信号日 {rep['n_signal_days']} | "
          f"因子观测 {rep['factor_observations']:,}")
    if um.get("avg_size") is not None:
        print(f"  时点宇宙日均 {um['avg_size']} [{um.get('min_size')}~{um.get('max_size')}] "
              f"master {um.get('master_coverage')}/{um.get('base_size')}")

    # IC 衰减表
    print("\nIC 衰减（横截面 Spearman，前向收益 vs 因子 score）:")
    print(f"  {'周期':<6}{'mean IC':>10}{'IR':>8}{'t-stat':>9}{'正IC%':>8}{'天数':>7}")
    for h in rep["periods"]:
        s = rep["ic"][str(h)]
        if s["mean_ic"] is None:
            print(f"  {h}日{'':<3}{'—':>10}")
            continue
        print(f"  {h}日{'':<3}{s['mean_ic']:+.4f}{'':<4}"
              f"{(s['ic_ir'] or 0):+.2f}{'':<4}"
              f"{(s['t_stat'] or 0):+.1f}{'':<3}"
              f"{s['pct_positive']:.0f}%{'':<4}{s['n_days']}")

    # 分位收益
    pp = rep["primary_period"]
    qr = rep["quantile_returns"]
    qstr = "  ".join(f"Q{i+1} {(v*1):+.2f}%" for i, v in enumerate(qr))
    print(f"\n分位收益（前向{pp}日，{rep['n_quantiles']}分位，Q1 最弱→Q{rep['n_quantiles']} 最强）:")
    print(f"  {qstr}")
    sp = rep["quantile_spread"]
    mo = rep["quantile_monotonicity"]
    print(f"  多空价差(Q{rep['n_quantiles']}−Q1) {sp:+.2f}%   单调性(Spearman) {mo:+.2f}"
          if sp is not None else "  （分位收益样本不足）")

    # 换手
    tov = rep["turnover"]
    nq = rep["n_quantiles"]
    lo = tov.get("0"); hi = tov.get(str(nq - 1)); ls = rep["long_short_turnover"]
    print("\n换手（日均成员变动率，0=恒定 1=每日全换）:")
    print(f"  Q1 {lo}   Q{nq} {hi}   多空≈ {ls}"
          if lo is not None else "  （换手样本不足）")

    if save:
        out_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "data", "factor_diag"))
        os.makedirs(out_dir, exist_ok=True)
        rid = datetime.now().strftime("diag-%Y%m%d-%H%M%S")
        out = os.path.join(out_dir, f"{rid}.json")
        tmp = f"{out}.tmp{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, out)
        print(f"\n  结果已保存: {out}")


def _parse_tables(spec: str | None) -> list[str] | None:
    if not spec:
        return None
    return [t.strip() for t in spec.split(",") if t.strip()]


def run_export_csv(out_dir: str, tables: list[str] | None, all_tables: bool) -> None:
    from stockfu.services.io_csv import export_csv

    scope = "全部表" if all_tables else ("指定表" if tables else "市场表")
    print(f"导出 CSV（{scope}）→ {out_dir}/ …")
    export_csv(out_dir, tables=tables, all_tables=all_tables)


def run_import_csv(in_dir: str, tables: list[str] | None, all_tables: bool) -> None:
    from stockfu.services.io_csv import import_csv

    scope = "全部表" if all_tables else ("指定表" if tables else "市场表")
    print(f"从 CSV 合并导入（{scope}）← {in_dir}/ …")
    import_csv(in_dir, tables=tables, all_tables=all_tables)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="stockfu", description="StockFu·资产管理终端")
    p.add_argument("--serve", action="store_true",
                   help="启动 Web（FastAPI+前端；无其它子命令时默认即此模式）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--reload", action="store_true")
    p.add_argument("--init-db", action="store_true", help="初始化数据库并写入种子/演示数据")
    p.add_argument("--buy", nargs=3, metavar=("CODE", "SHARES", "PRICE"), help="买入: 代码 股数 价格")
    p.add_argument("--sell", nargs=3, metavar=("CODE", "SHARES", "PRICE"), help="卖出: 代码 股数 价格")
    p.add_argument("--date", default=None, help="交易日期 YYYY-MM-DD（默认今天）")
    p.add_argument("--holdings", action="store_true", help="查看当前持仓")
    p.add_argument("--reset", action="store_true", help="清空所有持仓和交易记录")
    p.add_argument("--fetch", action="store_true",
                   help="抓取截至 --date 的行情/分红/情绪（必带 --date YYYY-MM-DD；"
                        "非法日期报错，凌晨不再误判为未开盘日）")
    p.add_argument("--backfill", type=int, nargs="?", const=90, help="回填 K线 N 日（默认90；情绪因子建议1825）")
    p.add_argument("--backfill-factors", action="store_true",
                   help="回补 两融总量历史 + 个股两融近10天 + 股息率历史序列")
    p.add_argument("--backfill-limit", type=int, nargs="?", const=365,
                   help="回补 连板/涨停历史（默认365天，限速，慢，建议后台）")
    p.add_argument("--backfill-benchmark", action="store_true",
                   help="回补回测基准 sh000001 历史日线（首次部署用）")
    p.add_argument("--backfill-sw", action="store_true",
                   help="回补 31 个申万一级行业指数历史日线(akshare index_hist_sw;行业情绪/轮动前置)")
    p.add_argument("--backfill-sw-refresh", action="store_true",
                   help="强制重跑已成功的申万行业指数项")
    p.add_argument("--backfill-sector-pulse", action="store_true",
                   help="串行回补同花顺90行业历史日线(2020至今；资金流从每日快照开始积累)")
    p.add_argument("--backfill-etf-industry", action="store_true",
                   help="回补行业 ETF 历史日线(前复权 qfq：东财→腾讯;可交易轮动前置)")
    p.add_argument("--backfill-etf-refresh", action="store_true",
                   help="强制重跑已成功的 ETF 项")
    p.add_argument("--backfill-etf", action="store_true",
                   help="可恢复回补 ETF 前复权日线(INDEX+行业+SECTOR+自选;默认不断表)")
    p.add_argument("--clear-etf-data", action="store_true",
                   help="仅配合 --backfill-etf：先清空 ETF 表再回补（破坏性操作）")
    p.add_argument("--backfill-universe", action="store_true",
                   help="回补 security_master(list_date/board, baostock;时点宇宙前置)")
    p.add_argument("--audit-corporate-actions", action="store_true",
                   help="只读审计公司行为：按年覆盖、重复和金额/来源异常（正式回测前置）")
    p.add_argument("--corporate-action-start-year", type=int, default=2007,
                   help="配合 --audit-corporate-actions：起始年（默认2007）")
    p.add_argument("--corporate-action-end-year", type=int, default=None,
                   help="配合 --audit-corporate-actions：结束年（默认当年）")
    p.add_argument("--backfill-index-universe", action="store_true",
                   help="导入默认指数当前成分快照(只按文件日期写入；不伪造历史)")
    p.add_argument("--backfill-financial", action="store_true",
                   help="baostock 财务三表 PIT 回补（分段+每日配额+断点续传）")
    p.add_argument("--fin-interfaces", default=None,
                   help="逗号分隔接口：profit,growth,balance,operation,cashflow,dupont（默认全部）")
    p.add_argument("--fin-budget", type=int, default=40000,
                   help="每日调用配额（baostock 上限约 5 万/天，默认 40000 留余量）")
    p.add_argument("--fin-codes", default=None,
                   help="逗号分隔股票代码过滤（默认全部有行情的 A 股）")
    p.add_argument("--fin-prefetch", action="store_true",
                   help="只预取上市日期（query_stock_basic → security_master.list_date）")
    p.add_argument("--fin-status", action="store_true",
                   help="只打印财务回补进度统计")
    p.add_argument("--fin-year-from", type=int, default=2007,
                   help="财务数据起始年份（默认 2007）")
    p.add_argument("--backfill-index-universe-history", action="store_true",
                   help="逐交易日串行回补沪深300+中证500历史成分（baostock，待正式档案核验）")
    p.add_argument("--backfill-index-universe-history-refresh", action="store_true",
                   help="强制重跑已 checkpoint 成功的指数成分交易日")
    p.add_argument("--backfill-index-universe-mirror", action="store_true",
                   help="导入中证1000可得的月度成分镜像（待正式档案核验，非日级完整）")
    p.add_argument("--backfill-star50-initial", action="store_true",
                   help="导入上交所公告附带的科创50官方初始样本（2020-07-23）")
    p.add_argument("--index-history-start", default="2006-01-01",
                   help="配合 --backfill-index-universe-history：起始日期 YYYY-MM-DD")
    p.add_argument("--index-history-end", default=None,
                   help="配合 --backfill-index-universe-history：结束日期 YYYY-MM-DD（默认今天）")
    p.add_argument("--index-codes", default=None,
                   help="配合 --backfill-index-universe：逗号分隔指数代码；默认历史宇宙指数")
    p.add_argument("--backfill-quote-status", action="store_true",
                   help="补历史 is_st/trade_status + 每只票最新交易日全量数据(baostock)")
    p.add_argument("--backfill-quote-status-refresh", action="store_true",
                   help="强制重跑当日已成功的行情状态证券")
    p.add_argument("--backfill-dividend", action="store_true",
                   help="回补全市场分红历史→dividend_event(baostock 主源/akshare兜底;可恢复长任务)")
    p.add_argument("--backfill-dividend-start-year", type=int, default=None,
                   help="分红历史回补的起始财年(仅与 --backfill-dividend 一起使用；默认2007)")
    p.add_argument("--backfill-dividend-refresh", action="store_true",
                   help="强制重跑已 checkpoint 成功的分红证券（默认仅重试失败/未完成项）")
    p.add_argument("--repair-known-dividend-conflicts", action="store_true",
                   help="事务化修复10个已审计分红冲突；随后 --backfill-dividend 只重试剩余失败项")
    p.add_argument("--backfill-adj-prices", action="store_true",
                   help="baostock 串行拉齐前复权/不复权/后复权→quote_snapshot "
                        "(*_qfq/*_raw/*_hfq);默认免费代理池;完成后清 dividend_yield 缓存")
    p.add_argument("--proxy-mode", default="free",
                   choices=["free", "clash", "direct"],
                   help="baostock 代理: free=公网免费池+Clash种子(默认); "
                        "clash=仅本机7891; direct=直连")
    p.add_argument("--no-socks", action="store_true",
                   help="等同 --proxy-mode direct（兼容旧参数）")
    p.add_argument("--full", action="store_true",
                   help="--backfill-adj-prices 强制全量重抓(默认断点续传:跳过 raw/hfq 已完成的 code)")
    p.add_argument("--clear-dividend-cache", action="store_true",
                   help="仅清 operator_result 中 dividend_yield 错误缓存")
    p.add_argument("--schedule", action="store_true", help="启动每日定时调度")
    p.add_argument("--clean-quotes", action="store_true", help="删除 quote_snapshot 里非交易日的错标记录")
    p.add_argument("--vacuum", action="store_true",
                   help="VACUUM INTO 原子重建主库(先备份 .bak.G09);停 daemon/回测时跑,回收空闲页")
    p.add_argument("--test-mail", action="store_true", help="立即生成多图并发一封测试邮件")
    p.add_argument("--scan-signals", action="store_true",
                   help="刷新并扫描信号日沪深300+中证500（必带 --date；策略默认读页面配置）")
    p.add_argument("--test-signal-mail", action="store_true",
                   help="立即把最近一次扫描生成推荐卡片并发送测试邮件")
    p.add_argument("--config", action="store_true", help="交互式配置向导：自选/抓取/重试/邮件")
    p.add_argument("--backtest", metavar="STRATEGY", default=None,
                   help="已禁用的 V1 回测入口；请使用 --backtest-v2")
    p.add_argument("--backtest-v2", metavar="ALPHA_ID", default=None,
                   help="V2 回测:alpha_id(如 dividend_low_vol_v2);"
                        "--codes hs300 或 historical_indices 使用历史成分；"
                        "复用 --start/--end/--cash")
    p.add_argument("--backtest-v2-segments", metavar="ALPHA_ID|all", default=None,
                   help="V2 正式分段回测:固定跑 full、2013-2019、2020-2026；"
                        "传 all=当前十策略，产物自动分目录保留")
    p.add_argument("--segments", default="all",
                   help="--backtest-v2-segments:区间选择，逗号分隔或 all；默认 all")
    p.add_argument("--segment-output-root", default="data/backtest/v2-segments",
                   help="分段 suite 根目录；每次运行自动新建 run-时间戳 子目录")
    p.add_argument("--segment-variant", default="daily",
                   help="分段产物变体目录名（如 daily/weekly/monthly）")
    p.add_argument("--resume-segment-suite", default=None,
                   help="续跑已有 V2 suite 目录；跳过已完成项并从未完成 checkpoint 接续")
    p.add_argument("--portfolio-v2", default=None,
                   help="V2 portfolio_policy_id(默认 cn_equity_top15_v2)")
    p.add_argument("--risk-v2", default=None,
                   help="V2 risk_policy_id(默认 no_overlay_v1)")
    p.add_argument("--history-origin", default=None,
                   help="V2 历史预热起点 YYYY-MM-DD(默认 eval_start 前 5 年)")
    p.add_argument("--observation-count", type=int, default=None,
                   help="V2 固定观察期交易日数；正式可比回测建议显式指定，避免延长 end 改变 formal_start")
    p.add_argument("--checkpoint", dest="checkpoint_path", default=None,
                   help="V2 按 --checkpoint-every 周期写完整断点状态 JSON；"
                        "必须同时固定 --observation-count")
    p.add_argument("--resume", dest="resume_from", default=None,
                   help="V2 从完整断点 JSON 继续；允许在同一口径下延长 --end")
    p.add_argument("--snapshot", dest="snapshot_path", default=None,
                   help="V2 复用既有数据快照 .db 文件（canonical 可复现运行用）；"
                        "省略则从主库新建快照")
    p.add_argument("--canonical", action="store_true",
                   help="V2 canonical 门禁：要求干净已提交工作树（dirty 直接拒绝，"
                        "不生成快照/不预载）；探索性运行省略本标志")
    # §4.8.4：默认 20 个交易日写一次完整 checkpoint（73MiB 工件不再默认每日重写）；
    # 中途可恢复性由 partial 工件 + append-only audit artifact 保证。
    p.add_argument("--checkpoint-every", type=int, default=20,
                   help="V2 每隔多少个交易日写一次完整断点，默认 20（§4.8.4）")
    p.add_argument("--update-backtests", action="store_true",
                   help="已禁用的 V1 全周期回测入口；请使用 --backtest-v2")
    p.add_argument("--strategies", default=None, metavar="IDS",
                   help="逗号分隔 strategy_id。"
                        "--update-backtests:省略=目录全部;"
                        "--recommend:必填。"
                        "例: cross_section_factor,dividend_cross_section")
    p.add_argument("--list-strategies", action="store_true",
                   help="列出 --update-backtests 可更新的策略目录后退出")
    p.add_argument("--dry-run", action="store_true",
                   help="配合 --update-backtests:只打印计划不实际跑回测")
    p.add_argument("--factor-diag", metavar="OPERATOR", default=None,
                   help="因子诊断算子ID（如 momentum / macd_cross）；单算子 IC/分位收益/换手/衰减，见 docs/BACKTEST.md")
    p.add_argument("--recommend", action="store_true",
                   help="空仓重建荐股(必填 --strategies;可选 --as-of/--cash)")
    p.add_argument("--v2-signal-mail", action="store_true",
                   help="V2 十策略单日评分 → 出图 → 发信(默认最新交易日;可 --as-of 指定)")
    p.add_argument("--v2-watchlist-recommend", action="store_true",
                   help="V2 十策略在自选股票范围评分并落盘荐股报告")
    p.add_argument("--no-send", action="store_true",
                   help="--v2-signal-mail:仅出图不发信(本地预览)")
    p.add_argument("--top-n", type=int, default=30,
                   help="--v2-signal-mail:展示 top N 只(默认30)")
    p.add_argument("--watchlist-review", action="store_true",
                   help="自选股多策略评价矩阵(默认 watchlist + active 策略;"
                        "可选 --codes/--add/--drop 临时调池;--strategies 任意入库 id)")
    p.add_argument("--from-pool", default="watchlist", metavar="POOL",
                   help="--watchlist-review 的基础池:watchlist(默认)/all/historical_indices"
                        " 或逗号代码;--codes 非空时被覆盖")
    p.add_argument("--add", action="append", default=None, metavar="CODE",
                   help="--watchlist-review:本次临时加入评价池(可多次;不写 DB)")
    p.add_argument("--drop", action="append", default=None, metavar="CODE",
                   help="--watchlist-review:本次临时移出评价池(可多次;不写 DB)")
    p.add_argument("--no-llm", action="store_true",
                   help="--watchlist-review:跳过 LLM 点评(只出量化评价)")
    p.add_argument("--as-of", default=None, metavar="YYYY-MM-DD",
                   help="信号日(荐股默认库内行情末日;严格 <=as_of 取数)")
    p.add_argument("--slip-bps", type=float, default=10.0,
                   help="荐股建议限价滑点(bps,默认10,与回测一致)")
    p.add_argument("--band-pct", type=float, default=1.0,
                   help="荐股执行可接受区间%%(默认±1)")
    p.add_argument("--max-gross", type=float, default=None,
                   help="荐股覆盖 rebalancer max_gross(默认用 catalog 参数)")
    p.add_argument("--with-sentiment", action="store_true",
                   help="荐股附加个股 fear/greed/heat(较慢)")
    p.add_argument("--write-cache", action="store_true",
                   help="荐股算子 miss 时写 operator_result(默认不写,防锁)")
    p.add_argument("--start", default=None,
                   help="回测/诊断/全周期更新起始日 YYYY-MM-DD"
                        "（--backtest 默认1年前;--update-backtests 默认 2021-01-01）")
    p.add_argument("--end", default=None,
                   help="回测/诊断/全周期更新结束日 YYYY-MM-DD"
                        "（--backtest 默认今天;--update-backtests 默认库内行情末日）")
    p.add_argument("--cash", type=float, default=1_000_000.0, help="回测初始资金（默认100万）")
    p.add_argument("--codes", default=None,
                   help="标的池:省略=自选; all/pool=大盘候选(~800); 或逗号代码列表")
    p.add_argument("--min-amount", type=float, default=None,
                   help="宇宙动态池:单日成交额门槛(元,如 50000000=5000万);默认关闭")
    p.add_argument("--valuation-basis", dest="valuation_basis",
                   choices=("qfq", "raw", "hfq"), default="qfq",
                   help="账户估值口径:qfq=前复权(默认,研究模式主线,已含分红再投);"
                        "raw=不复权+现金分红入账。研究模式详见 docs/BACKTEST.md §0.3")
    p.add_argument("--save", action="store_true", help="结果落盘（回测→data/backtest/ 诊断→data/factor_diag/）")
    p.add_argument("--periods", default=None,
                   help="因子诊断前向收益周期(交易日)，逗号分隔，默认 1,5,10,21")
    p.add_argument("--quantiles", type=int, default=5, help="因子诊断分位桶数（默认5）")
    p.add_argument("--params", default=None,
                   help="算子参数 JSON（如 '{\"window\":10}'）；默认用算子 PARAMS_SCHEMA")
    p.add_argument("--primary-period", type=int, default=None,
                   help="因子诊断分位收益/换手主周期(交易日，默认5)")
    p.add_argument("--export-csv", nargs="?", const="data", default=None, metavar="DIR",
                   help="导出市场数据为 CSV 到 DIR（默认 data/）；配合 --tables / --all")
    p.add_argument("--import-csv", nargs="?", const="data", default=None, metavar="DIR",
                   help="从 DIR 的 CSV 合并导入回库（默认 data/，upsert 不丢数据）")
    p.add_argument("--tables", default=None,
                   help="逗号分隔表名，覆盖默认市场表集（与 --export-csv/--import-csv 配合）")
    p.add_argument("--all", action="store_true",
                   help="导出/导入全部表（含个人持仓交易，慎提交 git）")
    return p


def main() -> None:
    from stockfu.config import setup_network

    setup_network()
    args = build_parser().parse_args()
    if args.reset:
        run_reset()
    elif args.holdings:
        run_holdings()
    elif args.buy:
        run_trade("buy", args.buy[0], args.buy[1], args.buy[2], args.date)
    elif args.sell:
        run_trade("sell", args.sell[0], args.sell[1], args.sell[2], args.date)
    elif args.init_db:
        run_init_db()
    elif args.fetch:
        run_fetch(args.date)
    elif args.backfill is not None:
        run_backfill(args.backfill)
    elif args.backfill_adj_prices:
        run_backfill_adj_prices(
            start=args.start,
            end=args.end,
            no_socks=args.no_socks,
            proxy_mode=getattr(args, "proxy_mode", "free"),
            full=getattr(args, "full", False),
        )
    elif args.clear_dividend_cache:
        run_clear_dividend_cache()
    elif args.backfill_factors:
        run_backfill_factors()
    elif args.backfill_limit is not None:
        run_backfill_limit(args.backfill_limit)
    elif args.backfill_benchmark:
        run_backfill_benchmark()
    elif args.backfill_sw:
        run_backfill_sw(refresh=args.backfill_sw_refresh)
    elif args.backfill_sector_pulse:
        run_backfill_sector_pulse()
    elif args.backfill_etf_industry:
        run_backfill_etf_industry(refresh=args.backfill_etf_refresh)
    elif args.backfill_etf:
        run_backfill_etf(refresh=args.backfill_etf_refresh, clear=args.clear_etf_data)
    elif args.backfill_universe:
        run_backfill_universe()
    elif args.audit_corporate_actions:
        run_audit_corporate_actions(args.corporate_action_start_year,
                                    args.corporate_action_end_year)
    elif args.repair_known_dividend_conflicts:
        run_repair_known_dividend_conflicts()
    elif args.backfill_financial:
        from stockfu.db import init_db
        init_db()  # create_all：财务表为新增，需建缺失表
        from stockfu.services.backfill_financial import (backfill_financial,
                                                         financial_status,
                                                         prefetch_listing_dates)
        if args.fin_status:
            st = financial_status()
            print(json.dumps(st, ensure_ascii=False, indent=2))
        else:
            ifaces = (args.fin_interfaces.split(",") if args.fin_interfaces else None)
            codes = (args.fin_codes.split(",") if args.fin_codes else None)
            if args.fin_prefetch:
                prefetch_listing_dates(codes)
            else:
                backfill_financial(interfaces=ifaces, codes=codes,
                                   daily_budget=args.fin_budget,
                                   year_from=args.fin_year_from)
    elif args.backfill_index_universe:
        run_backfill_index_universe(args.index_codes)
    elif args.backfill_index_universe_history:
        run_backfill_index_universe_history(args.index_history_start,
                                             args.index_history_end or date.today().isoformat(),
                                             refresh=args.backfill_index_universe_history_refresh)
    elif args.backfill_index_universe_mirror:
        run_backfill_index_universe_mirror(args.index_history_start,
                                            args.index_history_end or date.today().isoformat())
    elif args.backfill_star50_initial:
        run_backfill_star50_initial()
    elif args.backfill_quote_status:
        run_backfill_quote_status(refresh=args.backfill_quote_status_refresh)
    elif args.backfill_dividend:
        run_backfill_dividend(args.backfill_dividend_start_year,
                              refresh=args.backfill_dividend_refresh)
    elif args.schedule:
        run_schedule()
    elif args.clean_quotes:
        run_clean_quotes()
    elif args.vacuum:
        run_vacuum()
    elif args.test_mail:
        run_test_mail()
    elif args.scan_signals:
        run_signal_scan_cli(args.date, args.strategies)
    elif args.test_signal_mail:
        run_test_signal_mail()
    elif args.config:
        run_config()
    elif args.list_strategies:
        run_update_backtests(None, None, None, args.cash, False, list_only=True)
    elif args.update_backtests:
        run_update_backtests(
            args.strategies, args.start, args.end, args.cash,
            dry_run=args.dry_run, list_only=False,
        )
    elif args.v2_signal_mail:
        run_v2_signal_mail(args.as_of, args.no_send, args.top_n)
    elif args.v2_watchlist_recommend:
        run_v2_watchlist_recommend(args.as_of, args.top_n)
    elif args.recommend:
        run_recommend(
            args.strategies, args.as_of, args.cash,
            slip_bps=args.slip_bps, band_pct=args.band_pct,
            max_gross=args.max_gross, min_amount=args.min_amount,
            with_sentiment=args.with_sentiment, write_cache=args.write_cache,
        )
    elif args.watchlist_review:
        run_watchlist_review(
            args.strategies, args.as_of, args.from_pool, args.codes,
            add=args.add or [], drop=args.drop or [],
            with_sentiment=args.with_sentiment,
            with_llm=not args.no_llm,
            write_cache=args.write_cache,
        )
    elif args.backtest:
        run_backtest(args.backtest, args.start, args.end, args.cash, args.codes, args.save,
                     min_amount=args.min_amount,
                     valuation_basis=args.valuation_basis)
    elif args.backtest_v2_segments:
        run_v2_segmented_cli(
            args.backtest_v2_segments, args.start, args.end, args.cash,
            args.codes, args.portfolio_v2, args.risk_v2,
            args.history_origin, args.observation_count,
            args.checkpoint_path, args.resume_from, args.checkpoint_every,
            args.snapshot_path, args.canonical, args.segments,
            args.segment_output_root, args.segment_variant,
            args.resume_segment_suite,
        )
    elif args.backtest_v2:
        run_v2_backtest_cli(args.backtest_v2, args.start, args.end, args.cash,
                            args.codes, args.portfolio_v2, args.risk_v2,
                            args.history_origin, args.observation_count,
                            args.checkpoint_path, args.resume_from,
                            args.checkpoint_every, args.snapshot_path,
                            args.canonical)
    elif args.factor_diag:
        run_factor_diag(args.factor_diag, args.start, args.end, args.codes, args.params,
                        _parse_periods(args.periods), args.quantiles,
                        args.primary_period, args.save)
    elif args.export_csv is not None:
        run_export_csv(args.export_csv, _parse_tables(args.tables), args.all)
    elif args.import_csv is not None:
        run_import_csv(args.import_csv, _parse_tables(args.tables), args.all)
    else:
        # 无子命令时默认启动 Web（看板能力由前端承担；TUI 已移除）
        run_api(args.host, args.port, args.reload)


if __name__ == "__main__":
    main()
