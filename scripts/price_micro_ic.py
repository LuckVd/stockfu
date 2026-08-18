"""量价微观结构因子族 IC 快验（2026-08-18，接入前判别）。

候选五个"与现有策略风格不同"的量价因子（全部纯日线可实现，防未来函数）：

1. overnight_20d   隔夜收益均值（T+1 隔夜折价异象，中信建投/华安"昼夜分离"）
2. intraday_20d    日内收益均值（与隔夜拆解的正交维度）
3. cgo_60d         资本利得突出量（换手加权参考价，处置效应，Grinblatt-Han/广发）
4. amihud_20d      Amihud 非流动性（|ret|/amount 均值，东方证券十四）
5. wsplit_rev_20d  理想反转 W 切割（按单笔成交金额 D=amount/笔数 代理切 20 日收益，
                   东吴"订单簿的温度"——注意无成交笔数,用 amount/volume*股价 代理）

口径（与回测一致）：
- 宇宙:000300 历史成分并集,逐日 member_on PIT 过滤 + trade_status=1 + is_st=0
- 因子:全部 <=t 数据;qfq close 算收益,raw open/close 算隔夜(open_raw/close_raw)
- 未来收益:T 收盘后 1/5/20 交易日 qfq 收益;IC 一律截面 Spearman
- 分段:全段 / 2013-2019 / 2020-2026（对齐三段门禁的子段）
- amihud 用 amount(元) 归一,截面内 rank 消除量纲

用法:
    python3 scripts/price_micro_ic.py [--start 2013-06-01] [--end 2026-08-14] [--monthly]
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

WINDOWS = (20, 60)      # 因子窗口
FWD_H = (1, 5, 20)      # 未来收益视界(交易日)
MIN_N = 15              # 窗口最少有效样本
CGO_SPAN = 60           # CGO 递归窗(交易日)


def _conn() -> sqlite3.Connection:
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
    """code → 升序 [(d, close_qfq, open_raw, close_raw_prev?, volume, amount, turnover)].

    为算隔夜需要 raw open 与 raw close(前收);返回行含 open_raw/close_raw。
    """
    bars = {}
    chunk = 400
    codes = sorted(codes)
    for i in range(0, len(codes), chunk):
        part = codes[i:i + chunk]
        ph = ",".join("?" * len(part))
        rows = con.execute(
            f"SELECT asset_code, quote_date, close, open_raw, close_raw, "
            f"volume, amount, turnover, is_st, trade_status "
            f"FROM quote_snapshot WHERE asset_code IN ({ph}) "
            f"AND quote_date BETWEEN ? AND ? ORDER BY asset_code, quote_date",
            (*part, start.isoformat(), end.isoformat())).fetchall()
        for code, d, close, opr, cr, vol, amt, turn, is_st, ts in rows:
            bars.setdefault(code, []).append(
                (date.fromisoformat(str(d)[:10]), close, opr, cr,
                 vol, amt, turn, bool(is_st), int(ts or 1)))
    return bars


def trading_days(bars):
    days = set()
    for seq in bars.values():
        for row in seq:
            days.add(row[0])
    return sorted(days)


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


def compute_factors(bars, as_of_idx, code):
    """对 code 在 as_of(含) 截面上算五因子。bars[code] 升序列表,返回 dict 或 None。"""
    seq = bars[code]
    end_i = as_of_idx.get(code)
    if end_i is None:
        return None
    lo = max(0, end_i - max(WINDOWS[1], CGO_SPAN) - 5)
    win = seq[lo:end_i + 1]
    if len(win) < MIN_N:
        return None
    # 最近 20 日窗口
    w20 = win[-WINDOWS[0]:]
    if len(w20) < MIN_N:
        return None
    # 1) 隔夜收益均值: open_raw/prev close_raw - 1
    on_rets, in_rets = [], []
    for i in range(1, len(w20)):
        d, close, opr, cr, vol, amt, turn, _, _ = w20[i]
        pd_, pclose, _, pcr, pvol, pamt, _, _, _ = w20[i - 1]
        if opr and pcr:
            on = opr / pcr - 1
            on_rets.append(on)
            if close and pcr:  # qfq close 日内近似: close/close_prev - open/prev_close_raw... 用 raw 一致口径
                inr = (cr / pcr - 1) - on
                in_rets.append(inr)
    # 2) CGO: 换手加权参考价递归
    cgo = None
    try:
        span = win[-CGO_SPAN:]
        # 递归: RP_t = (1-turn_t)*RP_{t-1} + turn_t*P_t (换手率小数)
        rp = None
        for d, close, opr, cr, vol, amt, turn, _, _ in span:
            if close is None:
                continue
            t = (turn or 0) / 100.0
            t = min(max(t, 0), 0.99)
            if rp is None:
                rp = close
            else:
                rp = (1 - t) * rp + t * close
        if rp and span[-1][1]:
            cgo = span[-1][1] / rp - 1
    except Exception:
        cgo = None
    # 3) Amihud 20d: mean(|ret| / amount)
    amihud_vals = []
    for i in range(1, len(w20)):
        d, close, _, cr, _, amt, _, _, _ = w20[i]
        _, _, _, pcr, _, _, _, _, _ = w20[i - 1]
        if cr and pcr and amt:
            r = abs(cr / pcr - 1)
            amihud_vals.append(r / (amt / 1e8))
    amihud = sum(amihud_vals) / len(amihud_vals) if len(amihud_vals) >= MIN_N else None
    # 4) 理想反转 W 切割: D=amount/volume(单笔金额代理=每手金额,笔数缺失用vol)
    #    高 D 日收益和 - 低 D 日收益和 → 理想反转取负向(高D涨幅=假反转)
    wsplit = None
    try:
        rows_d = []
        for i in range(1, len(w20)):
            d, close, opr, cr, vol, amt, turn, _, _ = w20[i]
            _, _, _, pcr, _, _, _, _, _ = w20[i - 1]
            if cr and pcr and amt and vol:
                D = amt / vol  # 元/手(近似单笔强度)
                rows_d.append((D, cr / pcr - 1))
        if len(rows_d) >= MIN_N:
            rows_d.sort(key=lambda x: -x[0])
            half = len(rows_d) // 2
            hi = sum(r for _, r in rows_d[:half])
            lo_ = sum(r for _, r in rows_d[half:])
            wsplit = hi - lo_
    except Exception:
        wsplit = None
    return {
        "overnight": sum(on_rets) / len(on_rets) if len(on_rets) >= MIN_N else None,
        "intraday": sum(in_rets) / len(in_rets) if len(in_rets) >= MIN_N else None,
        "cgo": cgo,
        "amihud": amihud,
        "wsplit": wsplit,
    }


def fwd_return(bars, idx_map, code, end_i, horizon):
    seq = bars[code]
    j = end_i + horizon
    if j >= len(seq):
        return None
    c0, c1 = seq[end_i][1], seq[j][1]
    if not c0 or not c1:
        return None
    return c1 / c0 - 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2013-06-01")
    ap.add_argument("--end", default="2026-08-14")
    ap.add_argument("--monthly", action="store_true", help="月频截面(默认周频)")
    args = ap.parse_args()
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)

    con = _conn()
    members = load_memberships(con)
    # 预热需要 start 前 ~60 交易日 → 拉 start-5个月
    warm_start = date(start.year, start.month, 1)
    if start.month > 5:
        warm_start = date(start.year, start.month - 5, 1)
    else:
        warm_start = date(start.year - 1, start.month + 7, 1)
    bars = load_bars(con, set(members), warm_start, end)
    days = trading_days(bars)
    print(f"trading days {days[0]} ~ {days[-1]} ({len(days)}), codes={len(bars)}")

    # 每个截面的 idx map: code → 序列内最后 <= as_of 的下标
    # 预先构建 code → {date: idx}
    date_idx = {c: {row[0]: i for i, row in enumerate(seq)} for c, seq in bars.items()}

    # 截面日:月频=每月末交易日;周频=每周最后交易日
    cross_days = []
    for d in days:
        if d < start or d > end:
            continue
        cross_days.append(d)
    if args.monthly:
        md = {}
        for d in days:
            if d < start or d > end:
                continue
            md[(d.year, d.month)] = d
        cross_days = sorted(md.values())

    factors = ["overnight", "intraday", "cgo", "amihud", "wsplit"]
    # 分段
    segs = {"full": (start, end),
            "2013-2019": (start, date(2019, 12, 31)),
            "2020-2026": (date(2020, 1, 1), end)}
    stats = {seg: {f: {h: [] for h in FWD_H} for f in factors} for seg in segs}

    n_cross = 0
    for d in cross_days:
        # as_of_idx: 每 code 的最后 <=d 下标
        vals = {f: [] for f in factors}
        fwds = {h: [] for h in FWD_H}
        for code, seq in bars.items():
            if not member_on(members.get(code, []), d):
                continue
            # 二分找 <=d 最后下标
            di = date_idx.get(code, {})
            if d not in di:
                continue
            end_i = di[d]
            row = seq[end_i]
            if row[8] != 1 or row[7]:  # trade_status / is_st
                continue
            fdict = compute_factors(bars, {code: end_i}, code)
            if fdict is None:
                continue
            for f in factors:
                vals[f].append(fdict[f])
            for h in FWD_H:
                fwds[h].append(fwd_return(bars, date_idx, code, end_i, h))
        n_cross += 1
        for seg, (s0, s1) in segs.items():
            if not (s0 <= d <= s1):
                continue
            for f in factors:
                for h in FWD_H:
                    ic = spearman(vals[f], fwds[h])
                    if ic == ic:
                        stats[seg][f][h].append(ic)

    print(f"\ncross-sections: {n_cross}")
    for seg in segs:
        print(f"\n== {seg} ==")
        print(f"{'factor':<12}" + "".join(f"IC@{h}d   ICIR@{h}d  pos%   " for h in FWD_H))
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
            print(f"{f:<12}" + "".join(cells))
    print("\n判读:|IC|>0.02 且分段方向一致 → 立因子;amihud/overnight 预期正IC(低流动性溢价/隔夜折价反向)")


if __name__ == "__main__":
    sys.exit(main())
