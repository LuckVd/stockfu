#!/usr/bin/env python3
"""提取 earnings_momentum_offense_v2 三段门禁结果，并与现有四套策略对比。

用法:
    python3 scripts/offense_segments_report.py [SUITE_DIR] [--markdown]

不写任何库/缓存；只读 suite.json 与 summary JSON。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

BACKTEST_ROOT = Path("data/backtest")

# 现有四套正式三段结果的 suite 目录（相对 data/backtest/）
BASELINE_SUITES = {
    "value_ep_bp_equal_v2": "v2-tuning/final-canonical/run-20260813-031042-065592",
    "dividend_income_history45_v2": "v2-tuning/final-canonical/run-20260813-033738-699450",
    "multi_factor_value_tilt_v2": "v2-tuning/final-canonical/run-20260813-035850-261517",
    "multi_factor_quality_v2": "v2-quality-gate/run-20260813-231950-887694",
}


def load_suite(path: Path) -> dict:
    return json.loads((path / "suite.json").read_text(encoding="utf-8"))


def extract_metrics(suite: dict) -> dict[str, dict[str, dict]]:
    """alpha_id -> segment_id -> metrics（含 benchmark/excess）"""
    out: dict[str, dict[str, dict]] = {}
    for e in suite.get("entries", []):
        alpha_id = e.get("alpha_id")
        seg = e.get("segment_id")
        m = dict(e.get("metrics") or {})
        m["status"] = e.get("status")
        m["effective_eval_end"] = e.get("effective_eval_end")
        out.setdefault(alpha_id, {})[seg] = m
    return out


def format_metric(m: dict, key: str, digits: int = 2, suffix: str = "") -> str:
    v = m.get(key)
    if v is None:
        return "—"
    return f"{v:.{digits}f}{suffix}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("suite_dir", nargs="?", default=None,
                    help="进攻策略 suite 目录（默认取 v2-segments 最新 run）")
    ap.add_argument("--markdown", action="store_true", help="输出 markdown 表格")
    args = ap.parse_args()

    if args.suite_dir:
        offense_dir = Path(args.suite_dir)
    else:
        runs = sorted((BACKTEST_ROOT / "v2-segments").glob("run-*"))
        if not runs:
            raise SystemExit("没有找到 v2-segments 下的 suite")
        offense_dir = runs[-1]

    offense_suite = load_suite(offense_dir)
    offense = extract_metrics(offense_suite)
    if "earnings_momentum_offense_v2" not in offense:
        raise SystemExit(f"suite 中缺少 earnings_momentum_offense_v2: {offense_dir}")

    print(f"# 进攻策略三段门禁结果 — suite {offense_dir.name}\n")
    print(f"状态: {offense_suite.get('status')} | canonical: {offense_suite.get('canonical')} | "
          f"快照: {offense_suite.get('snapshot', {}).get('path')} "
          f"(数据到 {offense_suite.get('snapshot', {}).get('data_end')}) | "
          f"observation_count: {offense_suite.get('observation_count')}")
    print(f"git 提交: {offense_suite.get('git_commit') or offense_suite.get('commit')}")

    seg_order = ["full", "2013-2019", "2020-2026"]
    if args.markdown:
        print("\n## earnings_momentum_offense_v2\n")
        print("| 段 | 总收益 | 年化 | 最大回撤 | Sharpe | 基准 | 超额 | 年化换手 | 状态 |")
        print("|---|---:|---:|---:|---:|---:|---:|---:|---|")
        for seg in seg_order:
            m = offense["earnings_momentum_offense_v2"].get(seg)
            if not m:
                print(f"| {seg} | — | — | — | — | — | — | — | 缺失 |")
                continue
            print(f"| {seg} | {format_metric(m,'total_return')}% | {format_metric(m,'annualized')}% "
                  f"| {format_metric(m,'max_drawdown')}% | {format_metric(m,'sharpe')} "
                  f"| {format_metric(m,'benchmark_return')}% | {format_metric(m,'excess')}% "
                  f"| {format_metric(m,'annualized_turnover_pct')}% | {m.get('status')} |")

        print("\n## 与现有四套对比（三段，格式: 年化/Sharpe/最大回撤/超额）\n")
        print("| 策略 | Full | 2013–2019 | 2020–2026 |")
        print("|---|---|---|---|")
        all_data = {"earnings_momentum_offense_v2": offense["earnings_momentum_offense_v2"]}
        for name, rel in BASELINE_SUITES.items():
            all_data[name] = extract_metrics(load_suite(BACKTEST_ROOT / rel)).get(name, {})
        for name, data in all_data.items():
            cells = []
            for seg in seg_order:
                m = data.get(seg)
                if not m:
                    cells.append("—")
                    continue
                cells.append(
                    f"{format_metric(m,'annualized')}% / {format_metric(m,'sharpe')} / "
                    f"{format_metric(m,'max_drawdown')}% / +{format_metric(m,'excess')}%"
                )
            print(f"| {name} | {cells[0]} | {cells[1]} | {cells[2]} |")
    else:
        for seg in seg_order:
            m = offense["earnings_momentum_offense_v2"].get(seg)
            if not m:
                print(f"[{seg}] 缺失")
                continue
            print(f"[{seg}] ret={m.get('total_return')}% ann={m.get('annualized')}% "
                  f"dd={m.get('max_drawdown')}% sharpe={m.get('sharpe')} "
                  f"bench={m.get('benchmark_return')}% excess={m.get('excess')}% "
                  f"turn={m.get('annualized_turnover_pct')}% status={m.get('status')}")


if __name__ == "__main__":
    main()
