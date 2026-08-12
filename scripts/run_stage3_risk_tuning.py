#!/usr/bin/env python3
"""串行运行阶段三风险覆盖层候选。

固定阶段二选出的 Alpha、执行 policy、数据快照和三段区间；每个 job 只改变
风险覆盖层，避免把 Alpha 或执行层变化误当成风险控制改善。阶段三按策略串行
运行，保留每个风险候选的完整三段 suite。
"""
from __future__ import annotations

import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "data/snapshots/stockfu-2ee50075f50c.db"
OUTPUT_ROOT = ROOT / "data/backtest/v2-tuning/stage3-risk"
LOG_ROOT = ROOT / "data/logs"

# alpha_id, fixed execution policy, stable label
ALPHAS = (
    ("value_ep_bp_equal_v2", "pf_daily_top15_slow21_v2", "value_equal"),
    ("dividend_income_history45_v2", "pf_daily_top10_slow21_v2", "dividend_history45"),
    ("multi_factor_value_tilt_v2", "pf_daily_top15_r1_hold63_v2", "multi_value"),
)
RISKS = (
    ("portfolio_brake_v2", "brake"),
    ("market_regime_v2", "regime"),
    ("brake_regime_v2", "brake_regime"),
)


def main() -> int:
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for alpha_id, portfolio_id, alpha_label in ALPHAS:
        for risk_id, risk_label in RISKS:
            label = f"{alpha_label}_{risk_label}"
            log_path = LOG_ROOT / f"stage3_risk_{label}_{stamp}.log"
            cmd = [
                sys.executable, "-u", "main.py", "--backtest-v2-segments", alpha_id,
                "--codes", "hs300", "--portfolio-v2", portfolio_id,
                "--risk-v2", risk_id, "--snapshot", str(SNAPSHOT),
                "--observation-count", "271", "--segment-variant", f"stage3_{label}",
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
    print("第三阶段风险候选九组 suite 全部完成", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
