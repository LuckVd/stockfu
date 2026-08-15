"""龙虎榜事件 IC 快验脚本（2026-08-15，接入前判别）。

在近期窗口（默认 2026-01-01 → 2026-08-14）拉全市场龙虎榜每日明细（akshare
stock_lhb_detail_em，东财直连免代理），计算每个上榜事件在 T 日收盘（研究口径）
后 1/5/10/20 个交易日的 qfq 收益，按以下维度分组：

1. 机构净方向（从东财"解读"文本解析机构买入/卖出家数）
2. 龙虎榜净买额占总成交比（标准化后三分位）
3. 上榜日涨跌幅分组（反转交叠检验：大跌上榜 vs 大涨上榜）

目的：判断"机构行为"事件驱动是否有独立 alpha，还是仅仅是
短期反转/异动票的代理。有 signal 才投入建表 + 全历史回补。

用法：
    python scripts/lhb_ic_precheck.py [--start 2026-01-01] [--end 2026-08-14] [--cache data/lhb_events.json]

输出：事件统计 + 各分组事件后平均收益表。数据源为接口快照，结果只作判别用。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from stockfu.db import init_db

INST_PATTERN = re.compile(r"(\d+)家机构(买入|卖出)")


def fetch_events(start: date, end: date) -> list[dict]:
    """逐日拉取龙虎榜明细，合并为事件列表。"""
    import akshare as ak

    events: list[dict] = []
    d = start
    while d <= end:
        try:
            df = ak.stock_lhb_detail_em(
                start_date=d.strftime("%Y%m%d"), end_date=d.strftime("%Y%m%d"))
        except Exception as exc:  # noqa: BLE001
            print(f"  {d} 拉取失败: {exc}", flush=True)
            d += timedelta(days=1)
            continue
        if df is not None and not df.empty:
            for _, r in df.iterrows():
                events.append({
                    "date": str(r["上榜日"]),
                    "code": str(r["代码"]).zfill(6),
                    "name": str(r["名称"]),
                    "close": float(r["收盘价"]) if r["收盘价"] == r["收盘价"] else None,
                    "pct_chg": float(r["涨跌幅"]) if r["涨跌幅"] == r["涨跌幅"] else None,
                    "net_amount": float(r["龙虎榜净买额"]) if r["龙虎榜净买额"] == r["龙虎榜净买额"] else None,
                    "net_ratio": float(r["净买额占总成交比"]) if r["净买额占总成交比"] == r["净买额占总成交比"] else None,
                    "reason": str(r["上榜原因"]),
                    "interpret": str(r["解读"]),
                })
        d += timedelta(days=1)
        if len(events) % 500 == 0:
            print(f"  累计事件 {len(events)} ({d})", flush=True)
    return events


def inst_direction(interpret: str) -> int:
    """解析东财解读文本 → 机构净方向(+1 买入 / -1 卖出 / 0 无或持平)。"""
    buys = sells = 0
    for m in INST_PATTERN.finditer(interpret):
        n = int(m.group(1))
        if m.group(2) == "买入":
            buys += n
        else:
            sells += n
    if buys > sells:
        return 1
    if sells > buys:
        return -1
    return 0


def forward_returns(events: list[dict]) -> list[dict]:
    """事件 T 日收盘买入后 1/5/10/20 交易日 qfq 收益（主库 quote_snapshot）。

    返回与 events 一一对齐的 [{1: 收益|None, 5: ..., 10: ..., 20: ...}, ...]。
    """
    from stockfu.services.factors import quote_series

    out: list[dict] = []
    for e in events:
        t = date.fromisoformat(e["date"])
        closes = quote_series(e["code"], "close", 60, as_of=t)
        if len(closes) < 2:
            out.append({h: None for h in (1, 5, 10, 20)})
            continue
        base = closes[-1]
        if base is None or base <= 0:
            out.append({h: None for h in (1, 5, 10, 20)})
            continue
        row: dict = {}
        for h in (1, 5, 10, 20):
            px = _px_n_days_after(e["code"], t, h)
            row[h] = ((px / base - 1.0) * 100.0) if (px is not None and px > 0) else None
        out.append(row)
    return out


def _px_n_days_after(code: str, t: date, n: int):
    """t 日之后第 n 个交易日的 close（不含 t 日）。"""
    from stockfu.services.factors import quote_series_dates

    span = int((n + 5) * 1.5) + 30          # 保证 t 在窗口内且覆盖其后 n 根
    dates, closes = quote_series_dates(code, "close", span,
                                       as_of=t + timedelta(days=span))
    try:
        i = dates.index(t)
    except ValueError:
        return None
    j = i + n
    if j < len(closes):
        return closes[j]
    return None


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else float("nan")


def _h_vals(fwd: list[dict], idxs: list[int], h: int) -> list[float]:
    return [v for i in idxs if (v := fwd[i].get(h)) is not None]


def group_table(events: list[dict], fwd: list[dict],
                key_fn, label: str) -> None:
    groups: dict[str, list[int]] = defaultdict(list)
    for i, e in enumerate(events):
        groups[key_fn(e)].append(i)
    print(f"\n== {label} ==")
    print(f"{'分组':<22}{'N':>6}" + "".join(f"{'后'+str(h)+'日':>9}" for h in (1, 5, 10, 20)))
    for g, idxs in sorted(groups.items(), key=lambda kv: str(kv[0])):
        row = f"{str(g):<22}{len(idxs):>6}"
        for h in (1, 5, 10, 20):
            row += f"{mean(_h_vals(fwd, idxs, h)):>9.2f}"
        print(row)


def main() -> None:
    ap = argparse.ArgumentParser(description="龙虎榜事件 IC 快验")
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default="2026-08-14")
    ap.add_argument("--cache", default="data/lhb_events.json")
    args = ap.parse_args()

    init_db()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    cache = Path(args.cache)

    if cache.is_file():
        events = json.loads(cache.read_text(encoding="utf-8"))
        ev_dates = [e["date"] for e in events]
        covers = bool(ev_dates) and min(ev_dates) <= start.isoformat() \
            and max(ev_dates) >= end.isoformat()
        if covers:
            print(f"缓存读取 {len(events)} 事件: {cache}")
        else:
            print(f"缓存窗口({min(ev_dates)}~{max(ev_dates)})不覆盖 "
                  f"{start}~{end}，重新拉取", flush=True)
            events = fetch_events(start, end)
            cache.write_text(json.dumps(events, ensure_ascii=False, indent=1),
                             encoding="utf-8")
            print(f"已缓存 {len(events)} 事件: {cache}")
    else:
        print(f"拉取龙虎榜 {start} → {end} …", flush=True)
        events = fetch_events(start, end)
        cache.write_text(json.dumps(events, ensure_ascii=False, indent=1),
                         encoding="utf-8")
        print(f"已缓存 {len(events)} 事件: {cache}")

    if not events:
        print("无事件，退出")
        sys.exit(1)

    codes = {e["code"] for e in events}
    dates = sorted({e["date"] for e in events})
    print(f"\n事件 {len(events)} 条 | 股票 {len(codes)} 只 | 覆盖 {dates[0]} → {dates[-1]}")

    # 机构方向分布
    inst = defaultdict(int)
    for e in events:
        inst[inst_direction(e["interpret"])] += 1
    print(f"机构净方向: 买入 {inst[1]} | 卖出 {inst[-1]} | 无机构/持平 {inst[0]}")

    # 事件后收益
    print("计算事件后收益（T 日收盘口径, qfq）…", flush=True)
    fwd = forward_returns(events)
    n_ok = sum(1 for r in fwd if r.get(1) is not None)
    print(f"有完整前向收益的事件: {n_ok}/{len(events)}")

    # 全部事件基准
    print("\n== 全部事件 ==")
    print(f"{'N':>6}" + "".join(f"{'后'+str(h)+'日':>9}" for h in (1, 5, 10, 20)))
    print(f"{n_ok:>6}" + "".join(
        f"{mean(_h_vals(fwd, list(range(len(events))), h)):>9.2f}" for h in (1, 5, 10, 20)))

    group_table(events, fwd,
                lambda e: {1: "机构买入", -1: "机构卖出", 0: "无机构/持平"}[inst_direction(e["interpret"])],
                "机构净方向分组")
    group_table(events, fwd,
                lambda e: ("净买占比>2%" if (e["net_ratio"] or 0) > 2
                           else "净买占比<-2%" if (e["net_ratio"] or 0) < -2
                           else "净买占比-2~2%"),
                "净买额占比分组")
    group_table(events, fwd,
                lambda e: ("涨幅异动(>5%)" if (e["pct_chg"] or 0) > 5
                           else "跌幅异动(<-5%)" if (e["pct_chg"] or 0) < -5
                           else "温和(|<=5%)"),
                "上榜日涨跌幅分组(反转交叠检验)")


if __name__ == "__main__":
    main()
