"""从 V2 三段 suite 提取绩效指标（门禁判定用）。

用法:
    python3 scripts/segment_summary.py data/backtest/v2-segments/run-XXX

输出：每段 total_return / annualized / max_drawdown / sharpe / excess /
benchmark / 成交 / canonical 状态。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    suite = json.loads((root / "suite.json").read_text(encoding="utf-8"))
    print(f"suite: {suite.get('status')} | canonical: {suite.get('canonical')} | "
          f"obs_count: {suite.get('observation_count')}")
    for e in suite.get("entries", []):
        alpha = e.get("alpha_id")
        seg = e.get("segment")
        status = e.get("status")
        print(f"\n== {alpha} | {seg} | {status} ==")
        if status != "complete":
            print("  (未完成)")
            continue
        ck = root / seg / "checkpoint.json"
        if not ck.is_file():
            # 变体子目录
            cands = list((root / seg).glob("*/checkpoint.json"))
            ck = cands[0] if cands else None
        if not ck:
            print("  找不到 checkpoint.json")
            continue
        d = json.loads(ck.read_text(encoding="utf-8"))
        perf = d.get("performance") or d.get("result") or {}
        keys = ["total_return", "annualized", "max_drawdown", "sharpe",
                "benchmark_return", "excess", "sortino", "win_rate"]
        for k in keys:
            if k in perf:
                print(f"  {k}: {perf[k]}")
        meta = d.get("meta") or d.get("manifest") or {}
        if meta.get("canonical") is not None:
            print(f"  canonical: {meta.get('canonical')} | git_dirty: {meta.get('git_dirty')}")
        if "trades" in d:
            print(f"  trades: {len(d.get('trades', []))}")


if __name__ == "__main__":
    main()
