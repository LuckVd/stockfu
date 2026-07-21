#!/usr/bin/env python3
"""兼容入口:三策略空仓选股 → 转发 stockfu.services.recommend。

新入口请用:
  python3 main.py --recommend --strategies cross_section_factor,reversal_cross_section,dividend_cross_section --as-of 2026-07-17
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_STRATS = [
    "cross_section_factor",
    "reversal_cross_section",
    "dividend_cross_section",
]


def main():
    ap = argparse.ArgumentParser(description="空仓重建选股(转发 --recommend)")
    ap.add_argument("--as-of", default="2026-07-17")
    ap.add_argument("--max-gross", type=float, default=0.95)
    ap.add_argument("--min-amount", type=float, default=50_000_000)
    ap.add_argument("--no-sentiment", action="store_true")
    ap.add_argument(
        "--strategies",
        default=",".join(DEFAULT_STRATS),
        help="默认三 CS 策略;可用逗号覆盖",
    )
    args = ap.parse_args()

    from stockfu.db import init_db
    from stockfu.services.recommend import print_report, run_recommend

    init_db()
    ids = [x.strip() for x in args.strategies.split(",") if x.strip()]
    report = run_recommend(
        ids,
        as_of=date.fromisoformat(args.as_of),
        cash=1_000_000.0,
        max_gross=args.max_gross,
        min_amount=args.min_amount,
        with_sentiment=not args.no_sentiment,
        write_cache=False,
        save=True,
    )
    print_report(report)
    # 兼容旧产物路径:另存一份 picks_{as_of}.json 到 picks_717
    import json
    out_dir = ROOT / "data" / "reports" / "picks_717"
    out_dir.mkdir(parents=True, exist_ok=True)
    legacy = {
        "generated_at": report.get("generated_at"),
        "mode": "empty_portfolio_rebuild",
        "as_of": report.get("signal_date"),
        "note": "via services.recommend; 行情全市场末日以库内覆盖为准",
        "max_gross": args.max_gross,
        "reports": report.get("strategies"),
        "consensus": report.get("consensus"),
        "exec_date": report.get("exec_date"),
        "source": report.get("saved_to"),
    }
    out_path = out_dir / f"picks_{args.as_of}.json"
    out_path.write_text(
        json.dumps(legacy, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"兼容 JSON → {out_path}")


if __name__ == "__main__":
    main()
