#!/usr/bin/env python3
"""运行 V2 十策略 × 频率 × 固定样本区间矩阵。

默认矩阵为 10 alpha × {monthly, weekly, daily} ×
{full, 2013-2019, 2020-2026}，共 90 次独立回测。每次运行写入新的
``run-<timestamp>`` 目录，旧 suite 不覆盖。
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from stockfu.backtest.segments import FULL_SEGMENT  # noqa: E402
from stockfu.backtest.snapshot import descriptor_from_file, snapshot_engine  # noqa: E402
from stockfu.backtest.v2_engine import resolve_snapshot  # noqa: E402
from stockfu.backtest.v2_run import (  # noqa: E402
    default_universe,
    historical_full_universe,
    historical_full_universe_rules,
    historical_hs300_universe_rules,
    hs300_universe,
)
from stockfu.backtest.v2_suite import (  # noqa: E402
    research_deployments,
    run_segmented_backtests,
)
from stockfu.db import init_db, use_read_engine  # noqa: E402
from stockfu.services.universe import resolve_base_codes  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--alpha", default="all", help="alpha_id 或逗号列表，默认 all=十策略")
    p.add_argument("--frequency", default="all", help="monthly/weekly/daily 或 all")
    p.add_argument("--segments", default="all", help="full,2013-2019,2020-2026 或 all")
    p.add_argument("--codes", default="hs300",
                   help="hs300/historical_indices/代码列表，默认 hs300")
    p.add_argument("--snapshot", default=None, help="复用已有只读数据快照 .db")
    p.add_argument("--output-root", default="data/backtest/v2-segments",
                   help="suite 根目录，每次自动新建 run-时间戳")
    p.add_argument("--observation-count", type=int, default=271)
    p.add_argument("--checkpoint-every", type=int, default=20)
    p.add_argument("--cash", type=float, default=1_000_000.0)
    p.add_argument("--history-origin", default=None,
                   help="统一覆盖每段预热起点；默认按区间独立计算")
    p.add_argument("--canonical", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="只打印计划，不跑回测")
    return p


def main() -> None:
    args = build_parser().parse_args()
    deployments = research_deployments(args.alpha, args.frequency)
    if not deployments:
        raise SystemExit("没有待跑 deployment；请检查 --alpha/--frequency")
    segments = args.segments
    print(f"计划: {len(deployments)} 个 deployment × 区间选择 {segments!r}")
    for dep in deployments:
        print(f"  {dep.variant_id:7} {dep.alpha_id:28} {dep.portfolio_id or '(daily default)'}")
    if args.dry_run:
        return

    init_db()
    provided = descriptor_from_file(args.snapshot) if args.snapshot else None
    snapshot = resolve_snapshot(provided=provided, resume_from=None, snapshots_dir=None)
    with use_read_engine(snapshot_engine(snapshot)):
        low_codes = args.codes.lower() if args.codes else "hs300"
        universe_rules = None
        if low_codes == "hs300":
            codes = hs300_universe()
            universe_rules = historical_hs300_universe_rules()
        elif low_codes in ("historical_indices", "historical_index", "csi300_csi500"):
            codes = historical_full_universe()
            universe_rules = historical_full_universe_rules()
        elif args.codes:
            codes = resolve_base_codes(args.codes)
        else:
            codes = default_universe(FULL_SEGMENT.eval_start, FULL_SEGMENT.eval_end)

    history_origin = date.fromisoformat(args.history_origin) if args.history_origin else None
    run_root = Path(args.output_root) / f"run-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}"
    suite = run_segmented_backtests(
        deployments,
        output_root=run_root,
        segments=segments,
        codes=codes,
        universe_rules=universe_rules,
        history_origin=history_origin,
        initial_cash=args.cash,
        observation_count=args.observation_count,
        checkpoint_every=args.checkpoint_every,
        snapshot=snapshot,
        canonical=args.canonical,
    )
    print(f"完成: {len(suite.runs)} 次，suite manifest: {suite.manifest_path}")


if __name__ == "__main__":
    main()
