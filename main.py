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
    python main.py --schedule      # 每日定时调度
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
    print(f"✓ 数据库已初始化；种子自选 + 演示持仓已写入: {demo}")


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
    p.add_argument("--schedule", action="store_true", help="启动每日定时调度")
    p.add_argument("--clean-quotes", action="store_true", help="删除 quote_snapshot 里非交易日的错标记录")
    p.add_argument("--test-mail", action="store_true", help="立即生成多图并发一封测试邮件")
    p.add_argument("--config", action="store_true", help="交互式配置向导：自选/抓取/重试/邮件")
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
    elif args.schedule:
        run_schedule()
    elif args.clean_quotes:
        run_clean_quotes()
    elif args.test_mail:
        run_test_mail()
    elif args.config:
        run_config()
    elif args.serve:
        run_api(args.host, args.port, args.reload)
    else:
        run_tui()


if __name__ == "__main__":
    main()
