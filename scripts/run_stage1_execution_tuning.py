#!/usr/bin/env python3
"""串行运行三策略第一阶段（日调仓执行层）R2/R1 对照实验。

P0 使用已完成的统一基线；本脚本只跑每个 alpha 的两个新 policy，
每个 policy 固定 full、2013-2019、2020-2026 三段，避免多因子日频并发占满内存。
"""
from __future__ import annotations

import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "data/snapshots/stockfu-2ee50075f50c.db"
OUTPUT_ROOT = ROOT / "data/backtest/v2-tuning/stage1-execution"
LOG_ROOT = ROOT / "data/logs"

JOBS = (
    ("value_ep_bp_v2", "pf_daily_top15_r2_hold42_v2", "value_r2"),
    ("value_ep_bp_v2", "pf_daily_top15_r1_hold63_v2", "value_r1"),
    ("dividend_income_v2", "pf_daily_top10_r2_hold42_v2", "dividend_r2"),
    ("dividend_income_v2", "pf_daily_top10_r1_hold63_v2", "dividend_r1"),
    ("multi_factor_v2", "pf_daily_top15_r2_hold42_v2", "multi_r2"),
    ("multi_factor_v2", "pf_daily_top15_r1_hold63_v2", "multi_r1"),
)


def main() -> int:
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for alpha_id, portfolio_id, label in JOBS:
        log_path = LOG_ROOT / f"stage1_execution_{label}_{stamp}.log"
        cmd = [
            sys.executable, "-u", "main.py", "--backtest-v2-segments", alpha_id,
            "--codes", "hs300", "--portfolio-v2", portfolio_id,
            "--snapshot", str(SNAPSHOT), "--observation-count", "271",
            "--segment-variant", f"stage1_{label}",
            "--segment-output-root", str(OUTPUT_ROOT),
            "--cash", "1000000", "--checkpoint-every", "20", "--canonical",
        ]
        print(f"开始 {label}: {shlex.join(cmd)}", flush=True)
        with log_path.open("w", encoding="utf-8") as log:
            log.write(f"COMMAND: {shlex.join(cmd)}\n")
            log.flush()
            result = subprocess.run(cmd, cwd=ROOT, stdout=log,
                                    stderr=subprocess.STDOUT, check=False)
        if result.returncode != 0:
            print(f"失败 {label}: exit={result.returncode}; log={log_path}",
                  flush=True)
            return result.returncode
        print(f"完成 {label}: log={log_path}", flush=True)
    print("第一阶段 R2/R1 六组 suite 全部完成", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
