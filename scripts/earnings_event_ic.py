"""财报事件因子族 IC 快验（2026-08-18，PEAD/SUE/JOR 接入前判别）。

三个"盈余事件"因子（与现有 29 个 alpha 风格不同的基本面-量价混合维度）：

1. SUE_ttm   标准化未预期盈余：最近四季净利 TTM 环比变化 / 历史波动
             （Ball-Brown PEAD；无一致预期，用随机游走 SUE 代理）
2. JOR       盈余跳空：财报 pub_date 后首个交易日开盘跳空幅度（东方证券，IC 2.21%）
3. rec_acc   近 90 日公告累积漂移：公告后至 t 的超额收益（PEAD 持续性的价格确认）

口径：
- 宇宙:000300 历史成分 PIT 过滤 + trade_status=1 + is_st=0
- PIT 硬保证:只取 pub_date <= t 的财报行;JOR 用公告后首个交易日的 open_raw/前收
- 未来收益:T 后 1/5/20 日 qfq 收益;截面 Spearman IC
- 分段:full / 2013-2019 / 2020-2026

用法:
    python3 scripts/earnings_event_ic.py [--start 2013-06-01] [--end 2026-08-14]
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "stockfu.db"

FWD_H = (1, 5, 20)
SUE_LOOKBACK = 8          # SUE 历史波动用过去 8 个 TTM 变化
JOR_HOLD_DAYS = 63        # JOR/rec_acc 事件有效窗(约一个季度)


def _conn():
    con = sqlite3.connect(str(DB), timeout=60)
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("PRAGMA query_only=ON")
    return con


def load_memberships(con):
    rows = con.execute(
        "SELECT asset_code, effective_from, effective_to FROM index_constituent "
        "WHERE index_code='000300' ORDER BY asset_code, effective_from").fetchall()
    grouped = defaultdict(list)
    for code, fs, te in rows:
        start = date.fromisoformat(str(fs)[:10])
        end = date.fromisoformat(str(te)[:10]) if te else None
        grouped[code].append((start, end))
    out = {}
    for code, spans in grouped.items():
        merged = []
        for start, end in sorted(spans):
            if merged and start <= (merged[-1][1] or date.max):
                os_, oe = merged[-1]
                if oe is None or end is None:
                    merged[-1] = (os_, None)
                elif end > oe:
                    merged[-1] = (os_, end)
            else:
                merged.append((start, end))
        out[code] = merged
    return out


def member_on(spans, d):
    return any(s <= d and (e is None or d < e) for s, e in spans)


def load_bars(con, codes, start, end):
    bars = {}
    chunk = 400
    codes = sorted(codes)
    for i in range(0, len(codes), chunk):
        part = codes[i:i + chunk]
        ph = ",".join("?" * len(part))
        rows = con.execute(
            f"SELECT asset_code, quote_date, close, open_raw, close_raw, "
            f"is_st, trade_status FROM quote_snapshot "
            f"WHERE asset_code IN ({ph}) AND quote_date BETWEEN ? AND ? "
            f"ORDER BY asset_code, quote_date",
            (*part, start.isoformat(), end.isoformat())).fetchall()
        for code, d, close, opr, cr, is_st, ts in rows:
            bars.setdefault(code, []).append(
                (date.fromisoformat(str(d)[:10]), close, opr, cr,
                 bool(is_st), int(ts or 1)))
    return bars


def load_financials(con, codes):
    """code → 升序 [(pub_date, stat_date, year, quarter, net_profit)]（PIT 按 pub_date）。"""
    fins = {}
    chunk = 400
    codes = sorted(codes)
    for i in range(0, len(codes), chunk):
        part = codes[i:i + chunk]
        ph = ",".join("?" * len(part))
        rows = con.execute(
            f"SELECT asset_code, pub_date, stat_date, year, quarter, net_profit "
            f"FROM financial_profit WHERE asset_code IN ({ph}) "
            f"AND pub_date IS NOT NULL AND net_profit IS NOT NULL "
            f"ORDER BY asset_code, pub_date, stat_date",
            (*part,)).fetchall()
        for code, pd_, sd, y, q, np_ in rows:
            if pd_ is None or np_ is None:
                continue
            fins.setdefault(code, []).append(
                (date.fromisoformat(str(pd_)[:10]),
                 str(sd)[:10] if sd else None, y, q, float(np_)))
    return fins


def ttm_series(fins_code):
    """从季度净利构造 (pub_date, ttm) 升序序列:最近四季之和(按 stat_date 排)。"""
    # 以 (year, quarter) 去重(保留最新 pub)
    by_period = {}
    for pub, sd, y, q, np_ in fins_code:
        if y is None or q is None:
            continue
        by_period[(int(y), int(q))] = (pub, sd, np_)
    periods = sorted(by_period)
    out = []
    for i in range(3, len(periods)):
        window = periods[i - 3:i + 1]
        ttm = sum(by_period[p][2] for p in window)
        pub = max(by_period[p][0] for p in window)
        out.append((pub, ttm))
    return out


def compute_event_factors(ttms, bars_code, date_idx_code, t):
    """t 截面上算 SUE/JOR/rec_acc。bars_code 升序,ttms 为 (pub, ttm) 升序。"""
    if not ttms or not bars_code:
        return None
    # PIT:pub <= t 的最后一个 TTM 点
    idx = None
    for i, (pub, ttm) in enumerate(ttms):
        if pub <= t:
            idx = i
        else:
            break
    if idx is None:
        return None
    pub_t, ttm_now = ttms[idx]
    # SUE:ΔTTM / std(过去 SUE_LOOKBACK 个 ΔTTM)
    sue = None
    if idx >= 1:
        deltas = []
        for j in range(max(0, idx - SUE_LOOKBACK), idx + 1):
            deltas.append(ttms[j][1] - ttms[j - 1][1])
        if len(deltas) >= 4:
            mu = sum(deltas) / len(deltas)
            sd = (sum((x - mu) ** 2 for x in deltas) / max(len(deltas) - 1, 1)) ** 0.5
            if sd > 1e-9:
                sue = (ttm_now - ttms[idx - 1][1] - mu) / sd
    # 事件窗口:公告日距今超过 JOR_HOLD_DAYS 交易日 → JOR/rec_acc 失效
    if t not in date_idx_code:
        return {"sue": sue, "jor": None, "rec_acc": None}
    ti = date_idx_code[t]
    # 公告后首个交易日 = bars 中第一个 > pub 的日期
    jor = None
    rec_acc = None
    first_i = None
    seq = bars_code
    lo, hi = 0, len(seq) - 1
    # 二分找第一个 quote_date > pub
    while lo < hi:
        mid = (lo + hi) // 2
        if seq[mid][0] <= pub_t:
            lo = mid + 1
        else:
            hi = mid
    if seq[lo][0] > pub_t:
        first_i = lo
    if first_i is not None and ti > first_i and ti - first_i <= JOR_HOLD_DAYS:
        row_f = seq[first_i]
        prev = seq[first_i - 1] if first_i >= 1 else None
        if row_f[2] and prev and prev[3]:
            jor = row_f[2] / prev[3] - 1          # 跳空 = open_raw / prev close_raw
        c0, c1 = seq[first_i][1], seq[ti][1]
        if c0 and c1:
            rec_acc = c1 / c0 - 1                  # 公告首日至 t 漂移
    return {"sue": sue, "jor": jor, "rec_acc": rec_acc}


def spearman(xs, ys):
    pairs = [(x, y) for x, y in zip(xs, ys)
             if x is not None and y is not None and x == x and y == y]
    n = len(pairs)
    if n < 10:
        return float("nan")
    def ranks(vals):
        idx = sorted(range(len(vals)), key=lambda i: vals[i])
        r = [0.0] * len(vals)
        i = 0
        while i < len(idx):
            j = i
            while j + 1 < len(idx) and vals[idx[j + 1]] == vals[idx[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[idx[k]] = avg
            i = j + 1
        return r
    rx, ry = ranks([p[0] for p in pairs]), ranks([p[1] for p in pairs])
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx <= 0 or vy <= 0:
        return float("nan")
    return cov / math.sqrt(vx * vy)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2013-06-01")
    ap.add_argument("--end", default="2026-08-14")
    args = ap.parse_args()
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)

    con = _conn()
    members = load_memberships(con)
    warm_start = date(start.year - 1, start.month, 1)
    bars = load_bars(con, set(members), warm_start, end)
    fins = load_financials(con, set(members))
    ttms = {c: ttm_series(f) for c, f in fins.items()}
    date_idx = {c: {row[0]: i for i, row in enumerate(seq)} for c, seq in bars.items()}

    days = sorted({row[0] for seq in bars.values() for row in seq})
    md = {}
    for d in days:
        if start <= d <= end:
            md[(d.year, d.month)] = d
    cross_days = sorted(md.values())

    factors = ["sue", "jor", "rec_acc"]
    segs = {"full": (start, end),
            "2013-2019": (start, date(2019, 12, 31)),
            "2020-2026": (date(2020, 1, 1), end)}
    stats = {seg: {f: {h: [] for h in FWD_H} for f in factors} for seg in segs}

    for d in cross_days:
        vals = {f: [] for f in factors}
        fwds = {h: [] for h in FWD_H}
        for code, seq in bars.items():
            if not member_on(members.get(code, []), d):
                continue
            di = date_idx.get(code)
            if not di or d not in di:
                continue
            ti = di[d]
            row = seq[ti]
            if row[5] != 1 or row[4]:
                continue
            fd = compute_event_factors(ttms.get(code), seq, di, d)
            if fd is None:
                continue
            for f in factors:
                vals[f].append(fd[f])
            for h in FWD_H:
                j = ti + h
                if j < len(seq) and seq[ti][1] and seq[j][1]:
                    fwds[h].append(seq[j][1] / seq[ti][1] - 1)
                else:
                    fwds[h].append(None)
        for seg, (s0, s1) in segs.items():
            if not (s0 <= d <= s1):
                continue
            for f in factors:
                for h in FWD_H:
                    ic = spearman(vals[f], fwds[h])
                    if ic == ic:
                        stats[seg][f][h].append(ic)

    print(f"cross-sections: {len(cross_days)}")
    for seg in segs:
        print(f"\n== {seg} ==")
        print(f"{'factor':<10}" + "".join(f"IC@{h}d   ICIR@{h}d  pos%   " for h in FWD_H))
        for f in factors:
            cells = []
            for h in FWD_H:
                ics = stats[seg][f][h]
                if len(ics) < 5:
                    cells.append("   --        --       --   ")
                    continue
                m = sum(ics) / len(ics)
                sd = (sum((x - m) ** 2 for x in ics) / max(len(ics) - 1, 1)) ** 0.5 or 1e-9
                icir = m / sd * (len(ics) ** 0.5)
                pos = sum(1 for x in ics if x > 0) / len(ics)
                cells.append(f"{m:+.4f}    {icir:+5.2f}    {pos*100:3.0f}%   ")
            print(f"{f:<10}" + "".join(cells))


if __name__ == "__main__":
    sys.exit(main())
