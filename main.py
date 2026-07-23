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
    python main.py --update-backtests [--strategies a,b] [--start --end] [--dry-run] [--list-strategies]
        # 全周期重跑更新到最新(固化验收口径;不选策略=目录全部)
    python main.py --factor-diag OPERATOR [--start --end --codes --periods --quantiles --params --save]  # 因子诊断（见 docs/BACKTEST.md §11）
    python main.py --recommend --strategies a,b [--as-of] [--cash]  # 空仓重建荐股(次日开盘执行参考)
    python main.py --backfill-universe  # 回补 security_master(list_date/board, baostock)
    python main.py --backfill-quote-status  # 补历史状态 + 最新交易日全量(baostock)
    python main.py --backfill-adj-prices [--start] [--end]   # baostock 串行三复权(默认 Clash SOCKS)
    python main.py --clear-dividend-cache  # 清错误口径 dividend_yield 的 operator_result
"""
import argparse


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

def run_backfill_sw() -> None:
    from stockfu.scheduler.jobs import backfill_sw_index as _run

    print("回补 31 个申万一级行业指数历史日线（akshare index_hist_sw）…")
    print(f"✓ {_run()}")

def run_backfill_etf_industry() -> None:
    from stockfu.scheduler.jobs import backfill_industry_etf as _run

    print("回补行业 ETF 历史日线（前复权 qfq：东财→腾讯）…")
    print(f"✓ {_run()}")


def run_backfill_etf() -> None:
    """清空后全量重灌 ETF 前复权日线(INDEX+INDUSTRY+SECTOR+自选 fund_etf)。"""
    from stockfu.scheduler.jobs import backfill_etf_quotes, clear_etf_data, etf_universe_codes

    codes = etf_universe_codes()
    print("清空 ETF 相关表…")
    cleared = clear_etf_data()
    print(f"  cleared: {cleared}")
    print(f"全量回补 {len(codes)} 只 ETF 前复权日线（东财 qfq→腾讯 qfq）…")
    summary = backfill_etf_quotes(codes)
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


def run_backfill_quote_status() -> None:
    """补全:历史 is_st/trade_status + 每只票最新交易日全量数据(OHLCV/估值/状态)。"""
    from stockfu.db import init_db
    from stockfu.scheduler.jobs import backfill_quote_status
    from stockfu.services.universe import quote_status_coverage

    init_db()
    print("补全 quote_snapshot(历史状态 + 最新交易日全量, baostock) …")
    before = quote_status_coverage()
    print(f"  前: is_st_rate={before.get('is_st_rate')}  "
          f"trade_status_rate={before.get('trade_status_rate')}  rows={before.get('n_rows')}")
    r = backfill_quote_status()
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


def run_backfill_dividend() -> None:
    """回补全市场 A 股分红历史 → dividend_event 表。

    baostock query_dividend_data 主源(财年口径,近 10 年),akshare 兜底。
    resolve_base_codes('all') 取 quote_snapshot 全池(~800 票);按 ex_date 去重,
    幂等可重跑。baostock socket 轻量单线程,预计 10-20 分钟,建议后台跑。
    """
    from stockfu.db import init_db, session_scope
    from sqlalchemy import text
    from stockfu.services.universe import resolve_base_codes
    from stockfu.services import dividend as div_svc

    init_db()
    codes = resolve_base_codes("all")
    with session_scope() as s:
        before = s.exec(text("SELECT COUNT(*) FROM dividend_event")).all()[0][0]
    print(f"回补 {len(codes)} 只 A 股分红历史(baostock 主源 / akshare 兜底;前:{before} 行)…")
    new = errors = 0
    for i, c in enumerate(codes, 1):
        try:
            new += div_svc.persist_dividends(c)
        except Exception as e:  # noqa: BLE001
            errors += 1
            if errors <= 5:
                print(f"  ⚠ {c} 失败: {e}")
        if i % 50 == 0 or i == len(codes):
            print(f"  [{i}/{len(codes)}] 累计新增 {new} 条 (失败 {errors})")
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


def run_backtest(strategy: str, start: str | None, end: str | None,
                 cash: float, codes: str | None, save: bool,
                 strict: bool = True, min_amount: float | None = None) -> None:
    """回测：算子→策略→逐日 T+1 执行，输出绩效指标。

    策略由 app_config('active_strategy_id') 决定;此处 --backtest STRATEGY 设置它。
    --codes: 省略=自选; all/pool=大盘候选池(~800); 或逗号列表。
    strict(默认 True): 时点宇宙 + 涨跌停/滑点; --no-strict 对齐旧「有价即成交」。
    详见 docs/BACKTEST.md。
    """
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
    from stockfu.services.universe import resolve_base_codes

    end_d = end or date.today().isoformat()
    start_d = start or (date.today() - timedelta(days=365)).isoformat()
    code_list = resolve_base_codes(codes)

    scope = f"{len(code_list)}只票" + (" strict" if strict else " no-strict")
    print(f"回测 {strategy}  {start_d} → {end_d}  初始资金 {cash:,.0f}  ({scope}) …")
    universe_rules = None
    if min_amount is not None:
        from stockfu.services.universe import UniverseRules
        universe_rules = UniverseRules(min_amount_ma20=min_amount)
    r = _run(code_list, start_d, end_d, initial_cash=cash, strict=strict,
             universe_rules=universe_rules)
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
    score 走回测算子缓存(operator_result)，与回测互通复用。详见 docs/BACKTEST.md §11。
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
    p.add_argument("--backfill-etf-industry", action="store_true",
                   help="回补行业 ETF 历史日线(前复权 qfq：东财→腾讯;可交易轮动前置)")
    p.add_argument("--backfill-etf", action="store_true",
                   help="清空 ETF 表后全量重灌前复权日线(INDEX+行业+SECTOR+自选;东财qfq→腾讯qfq)")
    p.add_argument("--backfill-universe", action="store_true",
                   help="回补 security_master(list_date/board, baostock;时点宇宙前置)")
    p.add_argument("--backfill-quote-status", action="store_true",
                   help="补历史 is_st/trade_status + 每只票最新交易日全量数据(baostock)")
    p.add_argument("--backfill-dividend", action="store_true",
                   help="回补全市场分红历史→dividend_event(baostock query_dividend_data 主源/akshare兜底;红利因子前置,10-20分钟)")
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
    p.add_argument("--config", action="store_true", help="交互式配置向导：自选/抓取/重试/邮件")
    p.add_argument("--backtest", metavar="STRATEGY", default=None,
                   help="回测策略ID（如 macd_cross / bollinger_reversion）；详见 docs/BACKTEST.md")
    p.add_argument("--update-backtests", action="store_true",
                   help="全周期重跑更新到最新(固化验收口径;配合 --strategies 可选子集,省略=全部)")
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
                   help="因子诊断算子ID（如 momentum / macd_cross）；单算子 IC/分位收益/换手/衰减，见 docs/BACKTEST.md §11")
    p.add_argument("--recommend", action="store_true",
                   help="空仓重建荐股(必填 --strategies;可选 --as-of/--cash)")
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
    p.add_argument("--strict", dest="strict", action="store_true", default=True,
                   help="严谨模式(默认):时点宇宙+涨跌停/滑点")
    p.add_argument("--no-strict", dest="strict", action="store_false",
                   help="关闭宇宙/涨跌停/滑点(旧行为对照)")
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
        run_backfill_sw()
    elif args.backfill_etf_industry:
        run_backfill_etf_industry()
    elif args.backfill_etf:
        run_backfill_etf()
    elif args.backfill_universe:
        run_backfill_universe()
    elif args.backfill_quote_status:
        run_backfill_quote_status()
    elif args.backfill_dividend:
        run_backfill_dividend()
    elif args.schedule:
        run_schedule()
    elif args.clean_quotes:
        run_clean_quotes()
    elif args.vacuum:
        run_vacuum()
    elif args.test_mail:
        run_test_mail()
    elif args.config:
        run_config()
    elif args.list_strategies:
        run_update_backtests(None, None, None, args.cash, False, list_only=True)
    elif args.update_backtests:
        run_update_backtests(
            args.strategies, args.start, args.end, args.cash,
            dry_run=args.dry_run, list_only=False,
        )
    elif args.recommend:
        run_recommend(
            args.strategies, args.as_of, args.cash,
            slip_bps=args.slip_bps, band_pct=args.band_pct,
            max_gross=args.max_gross, min_amount=args.min_amount,
            with_sentiment=args.with_sentiment, write_cache=args.write_cache,
        )
    elif args.backtest:
        run_backtest(args.backtest, args.start, args.end, args.cash, args.codes, args.save,
                     strict=args.strict, min_amount=args.min_amount)
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
