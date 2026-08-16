"""换手/注意力 × 动量/反转 交叉 IC 快验脚本（2026-08-16，接入前判别）。

目标：在**大盘宇宙（沪深300 历史成分，PIT）**内检验"换手率维度"是否对
动量/反转有**风格独立**的区分度，回答三个问题：

1. 换手率水平（turn20）本身是否有截面预测力（注意力溢价/拥挤惩罚）？
2. 换手变化（turn_chg）是否携带增量信息？
3. 换手分层 × 动量分层的 3×3 交叉表——"高换手动量"（进攻）与
   "低换手反转"（防守）哪格有稳定的正收益？（换手作为动量/反转的
   条件过滤器）

口径（与回测一致，防未来函数）：
- 宇宙：index_constituent 000300 历史成分并集，逐日 member_on PIT 过滤
- 截面：当日有行情行 + trade_status=1 + is_st=0
- 因子：turn20（20 交易日换手均值）、turn_chg（turn20−turn60，%）、
  mom20/mom60（qfq 收盘收益 %）；全用 <=t 数据
- 未来收益：T 收盘后 1/5/20 个交易日 qfq 收益（close 列即 qfq）；
  **交叉表一律截面中性化**（减当日截面均值，剔除市场/beta 共同成分），
  并按时段分段（2021-2024 熊市段 / 2025-2026 反弹段）防单段主导
- 统计：每日截面内 Rank IC（Spearman），时间序列均值/ICIR/t/正占比；
  条件 IC = 换手三分位组内重新算动量 IC

用法：
    python3 scripts/turnover_attention_ic.py [--start 2021-01-01] [--end 2026-08-14]

输出：单因子 IC 表（全段+分段）+ 条件 IC + 中性化 3×3 交叉表。判别用，非回测。
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "stockfu.db"

# 因子窗口与未来收益视界
TURN_MA = (20, 60)          # 换手均线窗口（交易日）
MOM_WIN = (20, 60)          # 动量窗口（交易日）
FWD_H = (1, 5, 20)          # 未来收益视界（交易日）
MIN_TURN_N = 10             # 换手均线最少有效样本


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB), timeout=60)
    con.execute("PRAGMA temp_store=MEMORY")   # 沙箱内 SQLite 默认临时文件路径不可写
    con.execute("PRAGMA query_only=ON")
    return con


def load_memberships(con: sqlite3.Connection) -> dict[str, list[tuple[date, date | None]]]:
    """000300 历史成分区间：code → [(start, end|None), ...]（升序、合并）。"""
    rows = con.execute(
        "SELECT asset_code, effective_from, effective_to FROM index_constituent "
        "WHERE index_code='000300' ORDER BY asset_code, effective_from"
    ).fetchall()
    grouped: dict[str, list[tuple[date, date | None]]] = defaultdict(list)
    for code, fs, te in rows:
        start = date.fromisoformat(str(fs)[:10])
        end = date.fromisoformat(str(te)[:10]) if te else None
        grouped[code].append((start, end))
    out: dict[str, list[tuple[date, date | None]]] = {}
    for code, spans in grouped.items():
        merged: list[tuple[date, date | None]] = []
        for start, end in sorted(spans):
            if merged and start <= (merged[-1][1] or date.max):
                old_start, old_end = merged[-1]
                if old_end is None or end is None:
                    merged[-1] = (old_start, None)
                elif end > old_end:
                    merged[-1] = (old_start, end)
            else:
                merged.append((start, end))
        out[code] = merged
    return out


def member_on(spans: list[tuple[date, date | None]], d: date) -> bool:
    return any(s <= d and (e is None or d < e) for s, e in spans)


def load_bars(con: sqlite3.Connection, codes: set[str],
              start: date, end: date) -> dict[str, list]:
    """窗口内日线（升序）：code → [(date, close, turnover, is_st, trade_status), ...]。"""
    bars: dict[str, list] = {}
    chunk = 400
    for i in range(0, len(codes), chunk):
        part = sorted(codes)[i:i + chunk]
        ph = ",".join("?" * len(part))
        rows = con.execute(
            f"SELECT asset_code, quote_date, close, turnover, is_st, trade_status "
            f"FROM quote_snapshot WHERE asset_code IN ({ph}) "
            f"AND quote_date BETWEEN ? AND ? ORDER BY asset_code, quote_date",
            (*part, start.isoformat(), end.isoformat()),
        ).fetchall()
        for code, d, close, turn, is_st, ts in rows:
            bars.setdefault(code, []).append(
                (date.fromisoformat(str(d)[:10]), close, turn, bool(is_st), int(ts or 1)))
    return bars


def trading_days(bars: dict[str, list]) -> list[date]:
    """窗口内全局交易日（升序）。"""
    days: set[date] = set()
    for seq in bars.values():
        for row in seq:
            days.add(row[0])
    return sorted(days)


def spearman(xs: list[float], ys: list[float]) -> float:
    """Spearman 秩相关（去缺失，平局取平均秩）。"""
    pairs = [(x, y) for x, y in zip(xs, ys) if x == x and y == y]
    n = len(pairs)
    if n < 10:
        return float("nan")

    def ranks(vals: list[float]) -> list[float]:
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        rk = [0.0] * len(vals)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                rk[order[k]] = avg
            i = j + 1
        return rk

    rx = ranks([p[0] for p in pairs])
    ry = ranks([p[1] for p in pairs])
    mx = sum(rx) / n
    my = sum(ry) / n
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx <= 0 or syy <= 0:
        return float("nan")
    return sum((a - mx) * (b - my) for a, b in zip(rx, ry)) / math.sqrt(sxx * syy)


def tercile(x: float, q1: float, q2: float) -> int:
    """按截面分位点分 3 组（0/1/2）。"""
    if x <= q1:
        return 0
    if x <= q2:
        return 1
    return 2


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else float("nan")


def ic_report(ic_stats: dict[str, list[float]], label: str) -> None:
    print(f"  [{label}] N日={len(next(iter(ic_stats.values())))}")
    print(f"  {'因子':<10}{'MeanIC%':>9}{'ICIR':>7}{'t':>7}{'正占比%':>8}")
    for f, xs in ic_stats.items():
        n = len(xs)
        m = mean(xs)
        sd = math.sqrt(sum((x - m) ** 2 for x in xs) / max(n - 1, 1)) if n > 1 else float("nan")
        icir = m / sd if sd and sd > 0 else float("nan")
        t = m / sd * math.sqrt(n) if sd and sd > 0 else float("nan")
        pos = 100.0 * sum(1 for x in xs if x > 0) / n if n else float("nan")
        print(f"  {f:<10}{m * 100:>9.2f}{icir:>7.2f}{t:>7.2f}{pos:>8.1f}")


def main() -> None:
    ap = argparse.ArgumentParser(description="换手×动量 交叉 IC 快验（沪深300 宇宙）")
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-08-14")
    args = ap.parse_args()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    seg2_from = date(2025, 1, 1)

    con = _conn()
    memberships = load_memberships(con)
    codes = set(memberships)
    print(f"沪深300 历史成分并集: {len(codes)} 只 | 窗口 {start} → {end}")

    q_start = start - timedelta(days=150)
    q_end = end + timedelta(days=60)
    bars = load_bars(con, codes, q_start, q_end)
    con.close()
    days = [d for d in trading_days(bars) if start <= d <= end]
    print(f"截面交易日: {len(days)} 天 | 有行情股票 {len(bars)} 只")

    t0 = datetime.now()
    cross: list[dict] = []
    for code, spans in memberships.items():
        seq = bars.get(code)
        if not seq:
            continue
        ds = [r[0] for r in seq]
        closes = [r[1] for r in seq]
        turns = [r[2] for r in seq]
        is_st = [r[3] for r in seq]
        ts = [r[4] for r in seq]
        n = len(seq)
        turn_ma: dict[tuple[int, int], float] = {}
        for w in TURN_MA:
            for i in range(n):
                lo = max(0, i - w + 1)
                vals = [turns[k] for k in range(lo, i + 1)
                        if turns[k] is not None and turns[k] == turns[k]]
                if len(vals) >= MIN_TURN_N:
                    turn_ma[(w, i)] = sum(vals) / len(vals)
        mom: dict[int, float] = {}
        fwd: dict[int, float] = {}
        rets: list[float] = []
        for i in range(n):
            c0 = closes[i]
            if c0 is None or c0 <= 0:
                continue
            if i > 0 and closes[i - 1] is not None and closes[i - 1] > 0:
                rets.append((c0 / closes[i - 1] - 1.0) * 100.0)
            else:
                rets.append(float("nan"))
            for w in MOM_WIN:
                j = i - w
                if j >= 0 and closes[j] is not None and closes[j] > 0:
                    mom[(w, i)] = (c0 / closes[j] - 1.0) * 100.0
            for h in FWD_H:
                j = i + h
                if j < n and closes[j] is not None and closes[j] > 0:
                    fwd[h] = (closes[j] / c0 - 1.0) * 100.0
        # vol20：20 日收益标准差（波动率控制变量，滚动）
        vol20: dict[int, float] = {}
        for i in range(n):
            lo = max(0, i - 19)
            vals = [rets[k] for k in range(lo, i + 1)
                    if k < len(rets) and rets[k] == rets[k]]
            if len(vals) >= 10:
                m = sum(vals) / len(vals)
                vol20[i] = math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals))
        for i in range(n):
            d = ds[i]
            if d < start or d > end:
                continue
            if not member_on(spans, d):
                continue
            if is_st[i] or ts[i] == 0:
                continue
            row: dict = {"d": d, "code": code}
            t20 = turn_ma.get((20, i))
            t60 = turn_ma.get((60, i))
            if t20 is not None:
                row["turn20"] = t20
                if t60 is not None:
                    row["turn_chg"] = t20 - t60
            if i in vol20:
                row["vol20"] = vol20[i]
            for w in MOM_WIN:
                if (w, i) in mom:
                    row[f"mom{w}"] = mom[(w, i)]
            for h in FWD_H:
                if h in fwd:
                    row[f"fwd{h}"] = fwd[h]
            cross.append(row)
    print(f"截面样本: {len(cross)} 条 | 预计算耗时 {datetime.now() - t0}", flush=True)

    if len(cross) < 1000:
        print("样本不足，退出", file=sys.stderr)
        sys.exit(1)

    by_day: dict[date, list[dict]] = defaultdict(list)
    for row in cross:
        by_day[row["d"]].append(row)

    # ── A. 单因子 Rank IC（全段 + 分时段，fwd5 与 fwd20 双视界）──
    factors = ["turn20", "turn_chg", "mom20", "mom60"]
    print("\n== A. 单因子 Rank IC（每日截面 Spearman，对日期平均）==")
    for h in (5, 20):
        for label, d0, d1 in (("全段", start, end),
                              ("2021-2024 熊市段", start, seg2_from - timedelta(days=1)),
                              ("2025-2026 反弹段", seg2_from, end)):
            ic_stats: dict[str, list[float]] = {f: [] for f in factors}
            for d in sorted(by_day):
                if not (d0 <= d <= d1):
                    continue
                rows = [r for r in by_day[d] if f"fwd{h}" in r]
                for f in factors:
                    ic = spearman([r[f] for r in rows if f in r],
                                  [r[f"fwd{h}"] for r in rows if f in r])
                    if ic == ic:
                        ic_stats[f].append(ic)
            ic_report(ic_stats, f"fwd{h} | {label}")

    # ── B. 条件 IC：换手三分位组内的 mom20 IC（fwd5 与 fwd20）──
    print("\n== B. 条件 IC：换手三分位组内的 mom20 IC ==")
    for h in (5, 20):
        for label, d0, d1 in (("全段", start, end), ("2025-2026", seg2_from, end)):
            cond: dict[str, list[float]] = defaultdict(list)
            for d in sorted(by_day):
                if not (d0 <= d <= d1):
                    continue
                rows = [r for r in by_day[d]
                        if "turn20" in r and "mom20" in r and f"fwd{h}" in r]
                if len(rows) < 30:
                    continue
                tv = sorted(r["turn20"] for r in rows)
                q1t = tv[len(tv) // 3]
                q2t = tv[2 * len(tv) // 3]
                for tq in range(3):
                    sub = [r for r in rows if tercile(r["turn20"], q1t, q2t) == tq]
                    ic = spearman([r["mom20"] for r in sub], [r[f"fwd{h}"] for r in sub])
                    if ic == ic:
                        cond[tq].append(ic)
            print(f"  [fwd{h} | {label}]")
            for tq in range(3):
                xs = cond[tq]
                m = mean(xs)
                sd = math.sqrt(sum((x - m) ** 2 for x in xs) / max(len(xs) - 1, 1)) if len(xs) > 1 else float("nan")
                t = m / sd * math.sqrt(len(xs)) if sd and sd > 0 else float("nan")
                print(f"  换手组{tq}（低/中/高）: mom20 IC {m * 100:+.2f}%  t={t:+.2f}  N={len(xs)}")

    # ── B2. 归因：控制 mom20 后，turn20 的剩余预测力（组内 turn20 IC）──
    print("\n== B2. 归因：mom20 三分位组内的 turn20 IC（换手是否动量代理？）==")
    for h in (5, 20):
        for label, d0, d1 in (("全段", start, end), ("2025-2026", seg2_from, end)):
            cond: dict[str, list[float]] = defaultdict(list)
            for d in sorted(by_day):
                if not (d0 <= d <= d1):
                    continue
                rows = [r for r in by_day[d]
                        if "turn20" in r and "mom20" in r and f"fwd{h}" in r]
                if len(rows) < 30:
                    continue
                mv = sorted(r["mom20"] for r in rows)
                q1m = mv[len(mv) // 3]
                q2m = mv[2 * len(mv) // 3]
                for mq in range(3):
                    sub = [r for r in rows if tercile(r["mom20"], q1m, q2m) == mq]
                    ic = spearman([r["turn20"] for r in sub], [r[f"fwd{h}"] for r in sub])
                    if ic == ic:
                        cond[mq].append(ic)
            print(f"  [fwd{h} | {label}]")
            for mq in range(3):
                xs = cond[mq]
                m = mean(xs)
                sd = math.sqrt(sum((x - m) ** 2 for x in xs) / max(len(xs) - 1, 1)) if len(xs) > 1 else float("nan")
                t = m / sd * math.sqrt(len(xs)) if sd and sd > 0 else float("nan")
                print(f"  mom组{mq}（低/中/高）: turn20 IC {m * 100:+.2f}%  t={t:+.2f}  N={len(xs)}")

    # ── B3. 归因：控制 vol20 后，turn20 的剩余预测力（换手是否波动代理？）──
    print("\n== B3. 归因：vol20 三分位组内的 turn20 IC（换手是否波动率代理？）==")
    for h in (5, 20):
        for label, d0, d1 in (("全段", start, end), ("2025-2026", seg2_from, end)):
            cond: dict[str, list[float]] = defaultdict(list)
            for d in sorted(by_day):
                if not (d0 <= d <= d1):
                    continue
                rows = [r for r in by_day[d]
                        if "turn20" in r and "vol20" in r and f"fwd{h}" in r]
                if len(rows) < 30:
                    continue
                vv = sorted(r["vol20"] for r in rows)
                q1v = vv[len(vv) // 3]
                q2v = vv[2 * len(vv) // 3]
                for vq in range(3):
                    sub = [r for r in rows if tercile(r["vol20"], q1v, q2v) == vq]
                    ic = spearman([r["turn20"] for r in sub], [r[f"fwd{h}"] for r in sub])
                    if ic == ic:
                        cond[vq].append(ic)
            print(f"  [fwd{h} | {label}]")
            for vq in range(3):
                xs = cond[vq]
                m = mean(xs)
                sd = math.sqrt(sum((x - m) ** 2 for x in xs) / max(len(xs) - 1, 1)) if len(xs) > 1 else float("nan")
                t = m / sd * math.sqrt(len(xs)) if sd and sd > 0 else float("nan")
                print(f"  vol组{vq}（低/中/高）: turn20 IC {m * 100:+.2f}%  t={t:+.2f}  N={len(xs)}")

    # ── C. 中性化 3×3 交叉表（fwd20，减当日截面均值）──
    print("\n== C. turn20 × mom20 三分位交叉（fwd20 截面中性化，对日期平均）==")
    for label, d0, d1 in (("全段", start, end), ("2021-2024", start, seg2_from - timedelta(days=1)),
                          ("2025-2026", seg2_from, end)):
        cells: dict[tuple[int, int], list[float]] = defaultdict(list)
        for d in sorted(by_day):
            if not (d0 <= d <= d1):
                continue
            rows = [r for r in by_day[d] if "turn20" in r and "mom20" in r and "fwd20" in r]
            if len(rows) < 30:
                continue
            mkt = mean([r["fwd20"] for r in rows])
            tv = sorted(r["turn20"] for r in rows)
            mv = sorted(r["mom20"] for r in rows)
            q1t = tv[len(tv) // 3]
            q2t = tv[2 * len(tv) // 3]
            q1m = mv[len(mv) // 3]
            q2m = mv[2 * len(mv) // 3]
            for r in rows:
                cells[(tercile(r["turn20"], q1t, q2t),
                       tercile(r["mom20"], q1m, q2m))].append(r["fwd20"] - mkt)
        print(f"  [{label}] fwd20 中性化（%）:")
        print(f"  {'':<8}" + "".join(f"{f'mom{m}':>12}" for m in (0, 1, 2)))
        for tq in range(3):
            line = f"  {'turn' + str(tq):<8}"
            for mq in range(3):
                xs = cells[(tq, mq)]
                line += f"{mean(xs):>12.2f}" if xs else f"{'—':>12}"
            print(line)

    # ── D. 中性化配对差（fwd20）──
    print("\n== D. 换手条件化的动量/反转价差（fwd20 中性化）==")
    for label, d0, d1 in (("全段", start, end), ("2021-2024", start, seg2_from - timedelta(days=1)),
                          ("2025-2026", seg2_from, end)):
        cells: dict[tuple[int, int], list[float]] = defaultdict(list)
        for d in sorted(by_day):
            if not (d0 <= d <= d1):
                continue
            rows = [r for r in by_day[d] if "turn20" in r and "mom20" in r and "fwd20" in r]
            if len(rows) < 30:
                continue
            mkt = mean([r["fwd20"] for r in rows])
            tv = sorted(r["turn20"] for r in rows)
            mv = sorted(r["mom20"] for r in rows)
            q1t = tv[len(tv) // 3]
            q2t = tv[2 * len(tv) // 3]
            q1m = mv[len(mv) // 3]
            q2m = mv[2 * len(mv) // 3]
            for r in rows:
                cells[(tercile(r["turn20"], q1t, q2t),
                       tercile(r["mom20"], q1m, q2m))].append(r["fwd20"] - mkt)
        hi_hi = mean(cells[(2, 2)])
        lo_hi = mean(cells[(0, 2)])
        lo_lo = mean(cells[(0, 0)])
        hi_lo = mean(cells[(2, 0)])
        print(f"  [{label}] 高换手×高动量 {hi_hi:+.2f} vs 低换手×高动量 {lo_hi:+.2f}"
              f"（增量 {hi_hi - lo_hi:+.2f}）| 低换手×低动量 {lo_lo:+.2f} vs 高换手×低动量 {hi_lo:+.2f}"
              f"（增量 {lo_lo - hi_lo:+.2f}）| 多空 {hi_hi - lo_lo:+.2f}")


if __name__ == "__main__":
    main()
