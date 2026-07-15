"""StockFu · 资产管理终端 — 统一入口。

用法:
    python main.py                 # TUI 终端看板
    python main.py --init-db       # 初始化 + 种子自选 + 演示持仓
    python main.py --buy CODE N PRICE [--date YYYY-MM-DD]   # 买入
    python main.py --sell CODE N PRICE [--date]             # 卖出
    python main.py --holdings      # 查看持仓
    python main.py --reset         # 清空持仓和交易
    python main.py --backfill [N]  # 回填 K 线 N 日（默认90；情绪因子建议1825=5年）
    python main.py --backfill-factors    # 回补 两融总量历史 + 个股两融近10天 + 股息率历史序列
    python main.py --backfill-limit [N]  # 回补 连板/涨停历史（默认365天，限速1次/秒+断点续传，慢，建议后台跑）
    python main.py --fetch         # 每日抓取行情/分红/ETF + 算三层情绪指数
    python main.py --vacuum        # VACUUM 重建主库(回收空闲页,先备份;停 daemon/回测时跑)
    python main.py --schedule      # 每日定时调度
    python main.py --export-csv [DIR]  # 导出市场数据为 CSV（默认 data/，可入 git）
    python main.py --import-csv [DIR]  # 从 CSV 合并导入回库（换机同步；upsert 不丢数据）
    python main.py --backtest STRATEGY [--start --end --cash --codes --save]  # 回测（见 docs/BACKTEST.md）
    python main.py --serve         # FastAPI 服务
"""
import argparse


def run_tui() -> None:
    from stockfu.tui.app import StockFuApp

    StockFuApp().run()


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


def run_fetch() -> None:
    from stockfu.scheduler.jobs import run_scheduled_fetch

    print(f"✓ 抓取完成（今日行情+分红+指数）: {run_scheduled_fetch()}")


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

    print(f"回补回测基准 sh000001 历史日线…")
    print(f"✓ {_run()}")

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


def run_backtest(strategy: str, start: str | None, end: str | None,
                 cash: float, codes: str | None, save: bool) -> None:
    """回测：算子→策略→逐日 T+1 执行，输出绩效指标。

    策略由 app_config('active_strategy_id') 决定;此处 --backtest STRATEGY 设置它。
    详见 docs/BACKTEST.md。
    """
    from datetime import date, timedelta

    from stockfu.db import set_app_config
    set_app_config("active_strategy_id", strategy)
    from stockfu.ai.operators.registry import discover_and_register
    discover_and_register()
    from stockfu.backtest.scheduler import run as _run

    end_d = end or date.today().isoformat()
    start_d = start or (date.today() - timedelta(days=365)).isoformat()
    code_list = [c.strip() for c in codes.split(",") if c.strip()] if codes else None

    scope = f"{len(code_list)}只票" if code_list else "全部A股自选"
    print(f"回测 {strategy}  {start_d} → {end_d}  初始资金 {cash:,.0f}  ({scope}) …")
    r = _run(code_list, start_d, end_d, initial_cash=cash)
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
    print(f"✓ 总收益 {m.get('total_return')}% | 年化 {m.get('annualized')}% | "
          f"最大回撤 {m.get('max_drawdown')}% | 夏普 {m.get('sharpe')}{wr_str}{bench_str}{excess_str}\n"
          f"  交易 {m.get('trade_count')}笔 | 期末权益 {m.get('final_equity')}")
    if r.get("saved_to"):
        print(f"  结果已保存: {r['saved_to']}")


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
    p.add_argument("--serve", action="store_true", help="以 FastAPI 服务模式运行")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--reload", action="store_true")
    p.add_argument("--init-db", action="store_true", help="初始化数据库并写入种子/演示数据")
    p.add_argument("--buy", nargs=3, metavar=("CODE", "SHARES", "PRICE"), help="买入: 代码 股数 价格")
    p.add_argument("--sell", nargs=3, metavar=("CODE", "SHARES", "PRICE"), help="卖出: 代码 股数 价格")
    p.add_argument("--date", default=None, help="交易日期 YYYY-MM-DD（默认今天）")
    p.add_argument("--holdings", action="store_true", help="查看当前持仓")
    p.add_argument("--reset", action="store_true", help="清空所有持仓和交易记录")
    p.add_argument("--fetch", action="store_true", help="每日抓取并算三层情绪指数")
    p.add_argument("--backfill", type=int, nargs="?", const=90, help="回填 K线 N 日（默认90；情绪因子建议1825）")
    p.add_argument("--backfill-factors", action="store_true",
                   help="回补 两融总量历史 + 个股两融近10天 + 股息率历史序列")
    p.add_argument("--backfill-limit", type=int, nargs="?", const=365,
                   help="回补 连板/涨停历史（默认365天，限速，慢，建议后台）")
    p.add_argument("--backfill-benchmark", action="store_true",
                   help="回补回测基准 sh000001 历史日线（首次部署用）")
    p.add_argument("--schedule", action="store_true", help="启动每日定时调度")
    p.add_argument("--clean-quotes", action="store_true", help="删除 quote_snapshot 里非交易日的错标记录")
    p.add_argument("--vacuum", action="store_true",
                   help="VACUUM INTO 原子重建主库(先备份 .bak.G09);停 daemon/回测时跑,回收空闲页")
    p.add_argument("--test-mail", action="store_true", help="立即生成多图并发一封测试邮件")
    p.add_argument("--config", action="store_true", help="交互式配置向导：自选/抓取/重试/邮件")
    p.add_argument("--backtest", metavar="STRATEGY", default=None,
                   help="回测策略ID（如 macd_cross / bollinger_reversion）；详见 docs/BACKTEST.md")
    p.add_argument("--start", default=None, help="回测起始日 YYYY-MM-DD（默认1年前）")
    p.add_argument("--end", default=None, help="回测结束日 YYYY-MM-DD（默认今天）")
    p.add_argument("--cash", type=float, default=1_000_000.0, help="回测初始资金（默认100万）")
    p.add_argument("--codes", default=None, help="逗号分隔股票代码（默认全部A股自选）")
    p.add_argument("--save", action="store_true", help="回测结果落盘到 data/backtest/")
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
        run_fetch()
    elif args.backfill is not None:
        run_backfill(args.backfill)
    elif args.backfill_factors:
        run_backfill_factors()
    elif args.backfill_limit is not None:
        run_backfill_limit(args.backfill_limit)
    elif args.backfill_benchmark:
        run_backfill_benchmark()
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
    elif args.backtest:
        run_backtest(args.backtest, args.start, args.end, args.cash, args.codes, args.save)
    elif args.export_csv is not None:
        run_export_csv(args.export_csv, _parse_tables(args.tables), args.all)
    elif args.import_csv is not None:
        run_import_csv(args.import_csv, _parse_tables(args.tables), args.all)
    elif args.serve:
        run_api(args.host, args.port, args.reload)
    else:
        run_tui()


if __name__ == "__main__":
    main()
