#!/usr/bin/env python3
"""串行运行阶段二 Alpha 参数候选。

固定快照、沪深300、271 日观察窗和三段区间；每个 job 只改变 Alpha
权重或红利 mapping。执行 policy 已按阶段一结果固定，避免把执行层变化
误当成 Alpha 改善。多因子日频实验保持串行以控制内存。
"""
from __future__ import annotations

import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "data/snapshots/stockfu-2ee50075f50c.db"
OUTPUT_ROOT = ROOT / "data/backtest/v2-tuning/stage2-alpha"
LOG_ROOT = ROOT / "data/logs"

# alpha_id, fixed execution policy, label
JOBS = (
    ("value_ep_bp_ep70_v2", "pf_daily_top15_slow21_v2", "value_ep70"),
    ("value_ep_bp_equal_v2", "pf_daily_top15_slow21_v2", "value_equal"),
    ("value_ep_bp_bp60_v2", "pf_daily_top15_slow21_v2", "value_bp60"),
    ("dividend_income_abs75_v2", "pf_daily_top10_slow21_v2", "dividend_abs75"),
    ("dividend_income_history45_v2", "pf_daily_top10_slow21_v2", "dividend_history45"),
    ("multi_factor_value_tilt_v2", "pf_daily_top15_r1_hold63_v2", "multi_value"),
    ("multi_factor_momentum_tilt_v2", "pf_daily_top15_r1_hold63_v2", "multi_momentum"),
    ("multi_factor_lowvol_tilt_v2", "pf_daily_top15_r1_hold63_v2", "multi_lowvol"),
)


def main() -> int:
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for alpha_id, portfolio_id, label in JOBS:
        log_path = LOG_ROOT / f"stage2_alpha_{label}_{stamp}.log"
        cmd = [
            sys.executable, "-u", "main.py", "--backtest-v2-segments", alpha_id,
            "--codes", "hs300", "--portfolio-v2", portfolio_id,
            "--risk-v2", "no_overlay_v1", "--snapshot", str(SNAPSHOT),
            "--observation-count", "271", "--segment-variant", f"stage2_{label}",
            "--segment-output-root", str(OUTPUT_ROOT), "--cash", "1000000",
            "--checkpoint-every", "20", "--canonical",
        ]
        print(f"开始 {label}: {shlex.join(cmd)}", flush=True)
        with log_path.open("w", encoding="utf-8") as log:
            log.write(f"COMMAND: {shlex.join(cmd)}\n")
            log.flush()
            result = subprocess.run(
                cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=False
            )
        if result.returncode != 0:
            print(f"失败 {label}: exit={result.returncode}; log={log_path}", flush=True)
            return result.returncode
        print(f"完成 {label}: log={log_path}", flush=True)
    print("第二阶段 Alpha 候选八组 suite 全部完成", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
