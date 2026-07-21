#!/usr/bin/env python3
"""生成三策略 7.17 末日持仓图 + 交集分析图（PIL，无 matplotlib）。"""
from __future__ import annotations

import gzip
import json
import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "reports" / "holdings_717"
OUT.mkdir(parents=True, exist_ok=True)
ASOF = date(2026, 7, 17)

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
]


def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


F_TITLE = load_font(28)
F_SUB = load_font(16)
F_META = load_font(13)
F_HEAD = load_font(12)
F_CELL = load_font(12)
F_SMALL = load_font(11)

# dark theme
BG = (15, 23, 42)
CARD = (30, 41, 59)
HEADER_BG = (51, 65, 85)
WHITE = (248, 250, 252)
MUTED = (148, 163, 184)
DIM = (100, 116, 139)
GOLD = (251, 191, 36)
RED = (220, 38, 38)
GREEN = (22, 163, 74)
BLUE = (96, 165, 250)
PINK = (244, 114, 182)
TEAL = (45, 212, 191)


def pct_color(v):
    if v is None:
        return DIM
    if v > 0.05:
        return RED
    if v < -0.05:
        return GREEN
    return WHITE


def fmt_pct(v, nd=2):
    if v is None:
        return "—"
    return f"{v:+.{nd}f}%"


def fmt_num(v, nd=1):
    if v is None:
        return "—"
    return f"{v:.{nd}f}"


def connect():
    conn = sqlite3.connect(ROOT / "data" / "stockfu.db")
    conn.row_factory = sqlite3.Row
    return conn


def load_names(conn):
    names = {r[0]: r[1] for r in conn.execute("SELECT code, name FROM stock_basic")}
    for r in conn.execute("SELECT code, name FROM asset"):
        names.setdefault(r[0], r[1] or "")
    return names


def load_returns(conn, codes: set[str], as_of: date) -> dict:
    if not codes:
        return {}
    ph = ",".join("?" * len(codes))
    start = (as_of - timedelta(days=400)).isoformat()
    rows = conn.execute(
        f"""
        SELECT asset_code, quote_date, close, pct_chg
        FROM quote_snapshot
        WHERE asset_code IN ({ph}) AND quote_date >= ? AND quote_date <= ?
        ORDER BY asset_code, quote_date
        """,
        (*codes, start, as_of.isoformat()),
    ).fetchall()
    by = defaultdict(list)
    for r in rows:
        by[r["asset_code"]].append((r["quote_date"], r["close"], r["pct_chg"]))

    def pct(a, b):
        if a is None or b is None or a == 0:
            return None
        return (b / a - 1.0) * 100.0

    out = {}
    for code, series in by.items():
        if not series:
            continue
        i = len(series) - 1
        closes = [s[1] for s in series]
        pcts = [s[2] for s in series]
        c0 = closes[i]
        d1 = pcts[i] if pcts[i] is not None else (pct(closes[i - 1], c0) if i >= 1 else None)

        def lookback(n_td):
            j = max(0, i - n_td)
            return pct(closes[j], c0)

        out[code] = {
            "d1": d1,
            "w1": lookback(5),
            "m1": lookback(21),
            "y1": lookback(250),
            "close": c0,
        }
    return out


def load_sentiment(conn, codes: set[str]) -> dict:
    out = {c: {"fear": None, "greed": None, "heat": None} for c in codes}
    if not codes:
        return out
    ph = ",".join("?" * len(codes))
    rows = conn.execute(
        f"""
        SELECT scope, index_key, value, snap_date FROM index_snapshot
        WHERE level='stock' AND index_key IN ('fear','greed','heat')
          AND scope IN ({ph}) AND snap_date <= ?
        ORDER BY snap_date DESC
        """,
        (*codes, ASOF.isoformat()),
    ).fetchall()
    seen = set()
    for r in rows:
        key = (r["scope"], r["index_key"])
        if key in seen:
            continue
        seen.add(key)
        if r["scope"] in out:
            out[r["scope"]][r["index_key"]] = r["value"]
    return out


def load_run(run_id: str):
    with gzip.open(ROOT / "data" / "backtest" / f"{run_id}.json.gz", "rt", encoding="utf-8") as f:
        return json.load(f)


def holdings_of(run_id: str):
    d = load_run(run_id)
    last = d["holdings_curve"][-1]
    pos = [
        p
        for p in last["positions"]
        if (p.get("weight") or 0) > 0 and (p.get("shares") or 0) > 0
    ]
    pos.sort(key=lambda p: -(p.get("weight") or 0))
    return {
        "date": last["date"],
        "equity": last["equity"],
        "cash": last["cash"],
        "cash_pct": last["cash"] / last["equity"] * 100 if last["equity"] else 0,
        "positions": pos,
        "metrics": d.get("metrics") or {},
        "strategy_name": d.get("strategy_name") or "",
        "strategy_id": d.get("strategy_id") or "",
        "run_id": run_id,
    }


STRATS = [
    {
        "rank": 1,
        "sid": "cross_section_factor",
        "title": "横截面多因子",
        "subtitle": "反转 + 低波 + 价值 · cap_and_rank",
        "run_id": "run-20260719-171341",
        "accent": (26, 86, 219),
    },
    {
        "rank": 2,
        "sid": "reversal_cross_section",
        "title": "反转横截面",
        "subtitle": "反转 + RSI + 价值 · cap_and_rank",
        "run_id": "run-20260719-221731",
        "accent": (124, 58, 237),
    },
    {
        "rank": 3,
        "sid": "dividend_cross_section",
        "title": "红利横截面",
        "subtitle": "股息 + 低波 + 价值 · cap_and_rank",
        "run_id": "run-20260719-193259",
        "accent": (13, 148, 136),
    },
]


# column layout for strategy sheets (x start, width, key/header)
COLS = [
    (16, 36, "#", "idx"),
    (56, 70, "代码", "code"),
    (130, 90, "名称", "name"),
    (224, 58, "权重%", "w"),
    (286, 62, "股数", "shares"),
    (352, 80, "市值", "mkt"),
    (436, 72, "日涨跌", "d1"),
    (512, 72, "周涨跌", "w1"),
    (588, 72, "月涨跌", "m1"),
    (664, 78, "年涨跌", "y1"),
    (746, 52, "恐慌", "fear"),
    (802, 52, "贪婪", "greed"),
    (858, 52, "热度", "heat"),
    (914, 70, "收盘", "close"),
]

W = 1000
PAD_TOP = 140
ROW_H = 26
HEAD_H = 30
FOOT = 40


def rounded_rect(draw, xy, radius, fill, outline=None, width=1):
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)


def build_rows(positions, names, rets, sent):
    rows = []
    for p in positions:
        code = p["code"]
        r = rets.get(code) or {}
        s = sent.get(code) or {}
        rows.append(
            {
                "code": code,
                "name": (names.get(code) or "")[:6],
                "w": (p.get("weight") or 0) * 100,
                "shares": int(p.get("shares") or 0),
                "mkt": p.get("mkt_val") or 0,
                "close": p.get("close") or r.get("close"),
                "d1": r.get("d1"),
                "w1": r.get("w1"),
                "m1": r.get("m1"),
                "y1": r.get("y1"),
                "fear": s.get("fear"),
                "greed": s.get("greed"),
                "heat": s.get("heat"),
            }
        )
    return rows


def draw_strategy(s, names, rets, sent, path: Path):
    h = s["h"]
    rows = build_rows(h["positions"], names, rets, sent)
    n = len(rows)
    height = PAD_TOP + HEAD_H + n * ROW_H + FOOT
    img = Image.new("RGB", (W, height), BG)
    draw = ImageDraw.Draw(img)
    accent = s["accent"]

    # header card
    rounded_rect(draw, (12, 12, W - 12, 120), 12, CARD, outline=accent, width=2)
    draw.text((28, 22), f"#{s['rank']}  {s['title']}  ·  {s['sid']}", font=F_TITLE, fill=WHITE)
    draw.text((28, 56), s["subtitle"], font=F_SUB, fill=MUTED)
    met = h["metrics"]
    summary = (
        f"末日 {h['date']}  |  2021-01-01→2026-07-17  |  "
        f"总收益 {met.get('total_return')}%  超额 {met.get('excess')}%  "
        f"年化 {met.get('annualized')}%  回撤 {met.get('max_drawdown')}%  夏普 {met.get('sharpe')}  |  "
        f"权益 {h['equity']:,.0f}  现金 {h['cash_pct']:.1f}%  持仓 {len(rows)} 只"
    )
    draw.text((28, 82), summary, font=F_META, fill=(203, 213, 225))
    draw.text(
        (28, 100),
        f"run_id={h['run_id']}  ·  涨跌=quote≤{ASOF}  ·  恐/贪/热=个股情绪最新  ·  红涨绿跌  ·  非实盘荐股",
        font=F_SMALL,
        fill=DIM,
    )

    y = PAD_TOP
    # column header
    draw.rectangle((12, y, W - 12, y + HEAD_H), fill=accent)
    for x, wcol, lab, _ in COLS:
        # right-align numeric headers roughly by drawing at x+w-4 for rights
        draw.text((x, y + 7), lab, font=F_HEAD, fill=WHITE)

    y += HEAD_H
    for i, r in enumerate(rows, 1):
        bg = CARD if i % 2 == 0 else BG
        draw.rectangle((12, y, W - 12, y + ROW_H), fill=bg)

        def cell(key, text, color=WHITE, right=False, x0=None, ww=None):
            for cx, cw, _, k in COLS:
                if k == key:
                    if right:
                        # approximate right align
                        bbox = draw.textbbox((0, 0), text, font=F_CELL)
                        tw = bbox[2] - bbox[0]
                        draw.text((cx + cw - 8 - tw, y + 6), text, font=F_CELL, fill=color)
                    else:
                        draw.text((cx, y + 6), text, font=F_CELL, fill=color)
                    return

        cell("idx", f"{i}", MUTED)
        cell("code", r["code"], WHITE)
        cell("name", r["name"], WHITE)
        cell("w", f"{r['w']:.2f}", GOLD, right=True)
        cell("shares", f"{r['shares']:,}", MUTED, right=True)
        cell("mkt", f"{r['mkt']:,.0f}", MUTED, right=True)
        cell("d1", fmt_pct(r["d1"]), pct_color(r["d1"]), right=True)
        cell("w1", fmt_pct(r["w1"]), pct_color(r["w1"]), right=True)
        cell("m1", fmt_pct(r["m1"]), pct_color(r["m1"]), right=True)
        cell("y1", fmt_pct(r["y1"]), pct_color(r["y1"]), right=True)
        cell("fear", fmt_num(r["fear"]), BLUE, right=True)
        cell("greed", fmt_num(r["greed"]), PINK, right=True)
        cell("heat", fmt_num(r["heat"]), GOLD, right=True)
        close_s = f"{r['close']:.2f}" if r["close"] is not None else "—"
        cell("close", close_s, MUTED, right=True)
        y += ROW_H

    draw.text(
        (W // 2, height - 28),
        "StockFu · 回测末日持仓快照",
        font=F_SMALL,
        fill=DIM,
        anchor="mt",
    )
    img.save(path, "PNG", optimize=True)
    print(f"wrote {path} ({n} rows, {path.stat().st_size // 1024}KB)")


def draw_intersection(strats, names, rets, sent, path: Path):
    sets = {s["sid"]: {p["code"] for p in s["h"]["positions"]} for s in strats}
    s1 = sets["cross_section_factor"]
    s2 = sets["reversal_cross_section"]
    s3 = sets["dividend_cross_section"]
    inter_all = s1 & s2 & s3
    only12 = (s1 & s2) - s3
    only13 = (s1 & s3) - s2
    only23 = (s2 & s3) - s1
    only1 = s1 - s2 - s3
    only2 = s2 - s1 - s3
    only3 = s3 - s1 - s2

    wmap: dict[str, dict[str, float]] = {}
    for s in strats:
        for p in s["h"]["positions"]:
            wmap.setdefault(p["code"], {})[s["sid"]] = (p.get("weight") or 0) * 100

    def pack(codes, limit=None):
        rows = []
        for c in codes:
            r = rets.get(c) or {}
            se = sent.get(c) or {}
            ww = wmap.get(c, {})
            rows.append(
                {
                    "code": c,
                    "name": (names.get(c) or "")[:6],
                    "w1": ww.get("cross_section_factor"),
                    "w2": ww.get("reversal_cross_section"),
                    "w3": ww.get("dividend_cross_section"),
                    "d1": r.get("d1"),
                    "w1r": r.get("w1"),
                    "m1": r.get("m1"),
                    "y1": r.get("y1"),
                    "fear": se.get("fear"),
                    "greed": se.get("greed"),
                    "heat": se.get("heat"),
                    "close": r.get("close"),
                }
            )

        def avg_w(row):
            vals = [v for v in (row["w1"], row["w2"], row["w3"]) if v is not None]
            return sum(vals) / len(vals) if vals else 0

        rows.sort(key=avg_w, reverse=True)
        full_n = len(rows)
        if limit and len(rows) > limit:
            rows = rows[:limit]
        return rows, full_n

    sections = [
        ("三策略交集  1∩2∩3", inter_all, (245, 158, 11), None),
        ("仅 #1∩#2（不含红利）", only12, (167, 139, 250), None),
        ("仅 #1∩#3（不含反转CS）", only13, (52, 211, 153), None),
        ("仅 #2∩#3（不含横截面多因子）", only23, (56, 189, 248), None),
        ("仅 #1 独有（Top20）", only1, (100, 116, 139), 20),
        ("仅 #2 独有（Top20）", only2, (100, 116, 139), 20),
        ("仅 #3 独有（Top20）", only3, (100, 116, 139), 20),
    ]
    section_rows = []
    total = 0
    for title, codes, color, lim in sections:
        rows, full_n = pack(codes, lim)
        section_rows.append((title, rows, color, full_n))
        total += 1 + 1 + max(len(rows), 1)  # title + header + rows

    # ICOLS for intersection
    ICOLS = [
        (16, 30, "#", "idx"),
        (50, 70, "代码", "code"),
        (124, 80, "名称", "name"),
        (208, 52, "w1%", "w1"),
        (264, 52, "w2%", "w2"),
        (320, 52, "w3%", "w3"),
        (376, 68, "日涨跌", "d1"),
        (448, 68, "周涨跌", "w1r"),
        (520, 68, "月涨跌", "m1"),
        (592, 72, "年涨跌", "y1"),
        (668, 48, "恐慌", "fear"),
        (720, 48, "贪婪", "greed"),
        (772, 48, "热度", "heat"),
        (824, 64, "收盘", "close"),
        (892, 90, "出现", "tag"),
    ]

    height = 160 + total * ROW_H + len(sections) * 36 + FOOT
    img = Image.new("RGB", (W, height), BG)
    draw = ImageDraw.Draw(img)

    rounded_rect(draw, (12, 12, W - 12, 130), 12, CARD, outline=(245, 158, 11), width=2)
    draw.text((28, 22), "三策略持仓交集分析  ·  2026-07-17", font=F_TITLE, fill=WHITE)
    draw.text(
        (28, 56),
        "#1 横截面多因子  ·  #2 反转横截面  ·  #3 红利横截面   |   全周期回测末日持仓",
        font=F_SUB,
        fill=MUTED,
    )
    draw.text(
        (28, 84),
        f"1∩2∩3={len(inter_all)}  |  1∩2={len(s1 & s2)}  |  1∩3={len(s1 & s3)}  |  2∩3={len(s2 & s3)}  |  "
        f"仅1={len(only1)}  仅2={len(only2)}  仅3={len(only3)}  |  |#1|={len(s1)} |#2|={len(s2)} |#3|={len(s3)}",
        font=F_META,
        fill=WHITE,
    )
    draw.text(
        (28, 106),
        "w1/w2/w3=三策略各自权重%  ·  独有列表按均权 Top20  ·  红涨绿跌  ·  非实盘荐股",
        font=F_SMALL,
        fill=DIM,
    )

    y = 150

    def draw_header(y0, accent):
        draw.rectangle((12, y0, W - 12, y0 + HEAD_H), fill=HEADER_BG)
        for x, _, lab, _ in ICOLS:
            draw.text((x, y0 + 7), lab, font=F_HEAD, fill=WHITE)
        return y0 + HEAD_H

    for title, rows, color, full_n in section_rows:
        draw.rectangle((12, y, W - 12, y + 28), fill=color)
        dark_title = color[0] + color[1] + color[2] > 400
        draw.text(
            (24, y + 5),
            f"{title}  （{len(rows)}/{full_n}）" if len(rows) < full_n else f"{title}  （{full_n} 只）",
            font=F_SUB,
            fill=BG if dark_title else WHITE,
        )
        y += 32
        if not rows:
            draw.text((28, y + 4), "（空）", font=F_CELL, fill=DIM)
            y += ROW_H + 8
            continue
        y = draw_header(y, color)
        for i, r in enumerate(rows, 1):
            bg = CARD if i % 2 == 0 else BG
            draw.rectangle((12, y, W - 12, y + ROW_H), fill=bg)
            tags = []
            if r["w1"] is not None:
                tags.append("#1")
            if r["w2"] is not None:
                tags.append("#2")
            if r["w3"] is not None:
                tags.append("#3")

            def put(key, text, col=WHITE, right=False):
                for cx, cw, _, k in ICOLS:
                    if k != key:
                        continue
                    if right:
                        bbox = draw.textbbox((0, 0), text, font=F_CELL)
                        tw = bbox[2] - bbox[0]
                        draw.text((cx + cw - 6 - tw, y + 6), text, font=F_CELL, fill=col)
                    else:
                        draw.text((cx, y + 6), text, font=F_CELL, fill=col)

            put("idx", f"{i}", MUTED)
            put("code", r["code"])
            put("name", r["name"])
            put("w1", f"{r['w1']:.2f}" if r["w1"] is not None else "—", GOLD, True)
            put("w2", f"{r['w2']:.2f}" if r["w2"] is not None else "—", (196, 181, 253), True)
            put("w3", f"{r['w3']:.2f}" if r["w3"] is not None else "—", TEAL, True)
            put("d1", fmt_pct(r["d1"]), pct_color(r["d1"]), True)
            put("w1r", fmt_pct(r["w1r"]), pct_color(r["w1r"]), True)
            put("m1", fmt_pct(r["m1"]), pct_color(r["m1"]), True)
            put("y1", fmt_pct(r["y1"]), pct_color(r["y1"]), True)
            put("fear", fmt_num(r["fear"]), BLUE, True)
            put("greed", fmt_num(r["greed"]), PINK, True)
            put("heat", fmt_num(r["heat"]), GOLD, True)
            put("close", f"{r['close']:.2f}" if r["close"] is not None else "—", MUTED, True)
            put("tag", "+".join(tags), MUTED)
            y += ROW_H
        y += 10

    draw.text(
        (W // 2, height - 28),
        "StockFu · 交集分析（回测持仓）",
        font=F_SMALL,
        fill=DIM,
        anchor="mt",
    )
    img.save(path, "PNG", optimize=True)
    print(f"wrote {path} ({path.stat().st_size // 1024}KB)")


def main():
    conn = connect()
    names = load_names(conn)
    for s in STRATS:
        s["h"] = holdings_of(s["run_id"])

    all_codes: set[str] = set()
    for s in STRATS:
        for p in s["h"]["positions"]:
            all_codes.add(p["code"])

    print(f"codes={len(all_codes)}")
    rets = load_returns(conn, all_codes, ASOF)
    sent = load_sentiment(conn, all_codes)
    print(
        f"returns {sum(1 for c in all_codes if c in rets)}/{len(all_codes)}  "
        f"sent {sum(1 for c in all_codes if sent[c]['fear'] is not None)}/{len(all_codes)}"
    )

    for s in STRATS:
        path = OUT / f"{s['rank']}_{s['sid']}_holdings_20260717.png"
        draw_strategy(s, names, rets, sent, path)

    draw_intersection(STRATS, names, rets, sent, OUT / "4_intersection_analysis_20260717.png")
    print("DONE", OUT)


if __name__ == "__main__":
    main()
