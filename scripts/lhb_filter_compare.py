"""龙虎榜排雷过滤器组合级对照（2026-08-15）。

同一 alpha（默认 dividend_income_history45_v2）在相同窗口/快照下跑两组：
- 对照组：universe_rules 不含 lhb 过滤（基线）
- 实验组：universe_rules 加 exclude_lhb_net_sell_days=20、threshold=-2
  （近 20 日有大额净卖上榜的票剔除）

输出两组绩效（总收益/年化/Sharpe/回撤/超额/成交）对比，并统计实验组实际
剔除的票数与交易日分布——判断排雷过滤是否提升风险调整后收益。

用法：
    python -m scripts.lhb_filter_compare [--alpha dividend_income_history45_v2]
        [--start 2021-01-04] [--end 2026-07-16] [--window 20] [--threshold -2.0]
        [--snapshot data/snapshots/<id>.db]

注意：需要含 lhb_event 表的新快照（旧 bcf8e882afee 无此表）。
"""
from __future__ import annotations

import argparse
from datetime import date

from stockfu.backtest.snapshot import descriptor_from_file, snapshot_engine
from stockfu.backtest.v2_engine import resolve_snapshot
from stockfu.backtest.v2_run import run
from stockfu.db import init_db, use_read_engine
from stockfu.services.universe import UniverseRules

INDEXES = ("000300", "000905")


def main() -> None:
    ap = argparse.ArgumentParser(description="龙虎榜排雷过滤器对照")
    ap.add_argument("--alpha", default="dividend_income_history45_v2")
    ap.add_argument("--start", default="2021-01-04")
    ap.add_argument("--end", default="2026-07-16")
    ap.add_argument("--window", type=int, default=20)
    ap.add_argument("--threshold", type=float, default=-2.0)
    ap.add_argument("--snapshot", default=None)
    args = ap.parse_args()

    init_db()
    es = date.fromisoformat(args.start)
    ee = date.fromisoformat(args.end)

    from stockfu.backtest.v2_run import (
        historical_full_universe,
        historical_full_universe_rules,
    )
    provided = descriptor_from_file(args.snapshot) if args.snapshot else None
    snap = resolve_snapshot(provided=provided, resume_from=None, snapshots_dir=None)

    codes = historical_full_universe()
    base_rules = historical_full_universe_rules()

    with use_read_engine(snapshot_engine(snap)):
        # 基线
        res_base = run(args.alpha, eval_start=es, eval_end=ee, codes=codes,
                       universe_rules=base_rules, snapshot=snap)
        mb = res_base.metrics
        print("\n=== 基线(无过滤) ===")
        for k in ("total_return", "annualized", "max_drawdown", "sharpe",
                  "excess", "benchmark_return"):
            if k in mb:
                print(f"  {k}: {mb[k]}")
        print(f"  成交 {len(res_base.trades)} 笔 | 首单 {res_base.first_trade_date}")

        # 实验组
        rules = UniverseRules(
            universe_id=base_rules.universe_id,
            exclude_st=base_rules.exclude_st,
            require_trading=base_rules.require_trading,
            min_list_days=base_rules.min_list_days,
            index_codes=base_rules.index_codes,
            exclude_lhb_net_sell_days=args.window,
            lhb_net_sell_threshold=args.threshold,
        )
        res_f = run(args.alpha, eval_start=es, eval_end=ee, codes=codes,
                    universe_rules=rules, snapshot=snap)
        mf = res_f.metrics
        print(f"\n=== 排雷过滤(近{args.window}日 net_ratio<{args.threshold}% 剔除) ===")
        for k in ("total_return", "annualized", "max_drawdown", "sharpe",
                  "excess", "benchmark_return"):
            if k in mf:
                print(f"  {k}: {mf[k]}")
        print(f"  成交 {len(res_f.trades)} 笔 | 首单 {res_f.first_trade_date}")

        print("\n=== 差异(过滤 - 基线) ===")
        for k in ("total_return", "annualized", "max_drawdown", "sharpe"):
            if k in mb and k in mf:
                a, b = mb[k], mf[k]
                if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                    print(f"  {k}: {b - a:+.2f}")


if __name__ == "__main__":
    main()
