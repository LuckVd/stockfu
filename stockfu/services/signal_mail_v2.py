"""V2 策略评分邮件:长表 HTML 组装、Playwright 截图与 SMTP 发送。

粒度(方案①):直接用 ``V2SignalReport`` 的 strategy_score(已 [0,100]、50 中性),不做
再映射。跨策略分布差异通过图例里的校准元数据(P05/中位数/P95/饱和率/可交易占比)显式
暴露,提醒读者「各列分布不同,按列读、勿横向比绝对值」。

为控制邮件体积,默认展示「推荐榜单」:综合均分前 N(默认 30)∪ 每策略各自前 5 的并集,
去重后按均分排序;入选理由以中文标签标注在股票名下。N 可配。
"""
from __future__ import annotations

import html
import os
from itertools import islice
from typing import Iterable

from stockfu.services.mail import send_card_email
from stockfu.services.v2_signal import V2SignalReport

# V2 alpha 的表头简称 / 全称 / 一句话说明(粒度①图例 + 表头 title)。
# 调优后五套(邮件当前使用)在前,十策略研究 alpha 保留供对比复现。
V2_ALPHA_BRIEFS: dict[str, tuple[str, str, str]] = {
    # —— 调优后五套(邮件默认,与自选股荐股一致) ——
    "value_ep_bp_equal_v2": (
        "价值", "EP+BP 等权价值",
        "盈利收益率与账面市值比等权(各 0.5),绝对估值越低越好。",
    ),
    "dividend_income_history45_v2": (
        "高股息", "股息率历史分位",
        "TTM 股息率近 45 期历史相对排名,红利现金流因子。",
    ),
    "multi_factor_value_tilt_v2": (
        "多因子", "价值倾斜复合",
        "价值(EP+BP 0.6)+动量+低波复合,价值倾斜。",
    ),
    "multi_factor_quality_v2": (
        "质量增强", "多因子 + 质量极",
        "价值/动量/低波/红利复合 + 质量极(Roe 水平与稳定/毛利率/资产负债率,财务三表 PIT),"
        "2020+ 近期增强。",
    ),
    "earnings_momentum_offense_v2": (
        "盈利进攻", "盈利动量进攻",
        "盈利加速(Growth Accel)50%+价格动量 20%+低波 30% 复合,进攻腿;"
        "2026-08 纳入第五套正式荐股(vol8 配置)。",
    ),
    # —— 十策略研究 alpha(保留) ——
    "multi_factor_v2": (
        "多因子", "复合因子",
        "价值/动量/低波/红利/低β 的加权复合(长样本年化最高)。",
    ),
    "value_ep_bp_v2": (
        "价值", "EP+BP 绝对价值",
        "盈利收益率 + 账面市值比,绝对估值越低越好。",
    ),
    "dividend_income_v2": (
        "高股息", "TTM 股息率",
        "近 12 月税前股息率,红利现金流因子。",
    ),
    "low_volatility_pure_v2": (
        "低波", "20 日下行波动",
        "20 日下行波动率,低波防御因子。",
    ),
    "defensive_low_beta_v2": (
        "低β防御", "低贝塔红利",
        "低 β + 股息,防御性配置因子。",
    ),
    "momentum_jt_v2": (
        "动量12-1", "Jegadeesh-Titman 动量",
        "252 日动量剔除近 21 日,经典 12-1 动量。",
    ),
    "fifty_two_week_high_v2": (
        "52周高", "52 周新高",
        "接近 52 周最高价的程度,突破/趋势因子。",
    ),
    "trend_following_v2": (
        "趋势", "60 日趋势线性度",
        "60 日收益序列的线性拟合度,趋势跟踪因子。",
    ),
    "reversal_jl_v2": (
        "反转", "20 日短期反转",
        "20 日反转(Jegadeesh-Lehmann),超卖反弹因子。",
    ),
    "rsi_reversal_v2": (
        "RSI", "14 日 RSI 反转",
        "14 日 RSI 超卖反转,高频反转因子。",
    ),
}

_ROWS_PER_PAGE = 12


def _chunks(items: list, size: int) -> Iterable[list]:
    it = iter(items)
    while chunk := list(islice(it, size)):
        yield chunk


def _esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _score_cell(score: float | None, status: str) -> str:
    """0–100 单元格;not_tradable 用灰显(分数仍展示但弱化)。"""
    try:
        v = float(score)
    except (TypeError, ValueError):
        return "<td class='score-cell neutral'>—</td>"
    # 色档:>=60 偏买点(绿)、<=40 偏卖点(红)、中间中性;not_tradable 叠灰
    if v >= 60:
        cls = "high"
    elif v <= 40:
        cls = "low"
    else:
        cls = "neutral"
    if status != "tradable":
        cls = f"{cls} muted"
    title = "" if status == "tradable" else f" title='{status}'"
    return f"<td class='score-cell {cls}'{title}>{v:.0f}</td>"


def _calib_line(cal: dict) -> str:
    """图例里每策略的校准行(粒度①核心):分布统计。"""
    def fmt(x):
        return "—" if x is None else f"{float(x):.0f}"
    sat = cal.get("saturation_0_100")
    trad = cal.get("tradable_pct")
    sat_s = "—" if sat is None else f"{float(sat) * 100:.0f}%"
    trad_s = "—" if trad is None else f"{float(trad) * 100:.0f}%"
    return (
        f"<span class='cal'>P05 {fmt(cal.get('p05'))} · "
        f"中位 {fmt(cal.get('p50'))} · P95 {fmt(cal.get('p95'))}</span>"
        f"<span class='cal2'>饱和 {sat_s} · 可交易 {trad_s} · n={cal.get('n', 0)}</span>"
    )


def _legend_html(report: V2SignalReport) -> str:
    rows = []
    for aid in report.alpha_ids:
        short, full, brief = V2_ALPHA_BRIEFS.get(
            aid, (aid, aid.replace("_v2", "").replace("_", " "), "V2 因子策略")
        )
        rows.append(
            "<div class='legend-row'>"
            f"<b>{_esc(short)}</b>"
            f"<div><strong>{_esc(full)}</strong>: {_esc(brief)}"
            f"<br>{_calib_line(report.calibration.get(aid, {}))}</div>"
            "</div>"
        )
    return (
        "<section class='strategy-legend'>"
        "<h2>策略与校准（表头简称 · 各列分布不同，请按列读）</h2>"
        + "".join(rows) +
        "</section>"
    )


def _table_html(rows: list[dict], alpha_ids: list[str]) -> str:
    head = "".join(
        f"<th class='strategy-col' title='{_esc(V2_ALPHA_BRIEFS.get(a, (a, a, a))[1])}'>"
        f"{_esc(V2_ALPHA_BRIEFS.get(a, (a, a, a))[0])}</th>"
        for a in alpha_ids
    )
    body = []
    for row in rows:
        scores = row.get("scores") or {}
        incl = row.get("inclusion") or []
        incl_line = (
            f"<small class='incl'>{_esc(' · '.join(incl))}</small>" if incl else ""
        )
        stock = f"<td class='stock-cell'><b>{_esc(row.get('name') or row.get('code') or '—')}</b>" \
                f"<small>{_esc(row.get('code') or '')}</small>{incl_line}</td>"
        cells = [stock]
        for a in alpha_ids:
            cell = scores.get(a) or {}
            cells.append(_score_cell(cell.get("score"), cell.get("status", "tradable")))
        body.append("<tr>" + "".join(cells) + "</tr>")
    if not body:
        body.append(
            f"<tr><td class='table-empty' colspan='{1 + len(alpha_ids)}'>本页没有可展示的股票。</td></tr>"
        )
    return (
        "<table class='score-table'><thead><tr>"
        "<th class='stock-col'>股票</th>"
        f"{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"
    )


def build_v2_signal_mail_html(
    report: V2SignalReport, *, top_n: int = 30, list_rows: list[dict] | None = None
) -> str:
    """组装邮件 HTML。

    ``list_rows`` 为推荐榜单（综合前 top_n ∪ 各策略前 5，按均分排序）时用之，
    否则回退 ``report.rows[:top_n]``（均值 top N）。
    """
    rows = list_rows if list_rows is not None else report.rows[:top_n]
    pages = list(_chunks(rows, _ROWS_PER_PAGE)) or [[]]
    page_html = []
    for page_no, page_rows in enumerate(pages, 1):
        legend = _legend_html(report) if page_no == 1 else ""
        table = _table_html(page_rows, report.alpha_ids)
        desc = (
            f"推荐榜单 {len(rows)} 只（综合前 {top_n} ∪ 各策略前 5，按均分排序）"
            if list_rows is not None
            else f"展示 top {len(rows)} / 全宇宙 {report.universe_size} 只"
        )
        page_html.append(
            "<main class='signal-page'>"
            "<div class='page-head'>"
            "<div><h1>StockFu V2 策略评分</h1>"
            f"<p>{_esc(report.as_of.isoformat() if hasattr(report.as_of, 'isoformat') else report.as_of)} · "
            f"{_esc(desc)} · "
            "各策略独立 0–100 分，50 为中性</p></div>"
            f"<div class='page-no'>{page_no}/{len(pages)}</div></div>"
            f"{legend}{table}"
            "<footer>研究回测产物，不构成投资建议；评分不读取持仓、不承诺收益。"
            "各策略映射基不同（绝对锚点 vs 历史分位），列间分布有差异，请按列读。"
            "榜单=综合均分前 30 ∪ 每策略各自前 5，去重后按均分排序。</footer>"
            "</main>"
        )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>StockFu V2 策略评分</title>
<style>
*{{box-sizing:border-box}}html,body{{margin:0;background:#f4f0e6;color:#27231d;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif}}
body{{padding:24px}}.signal-page{{width:1280px;min-height:900px;margin:0 auto 24px;padding:28px 30px;background:#fffdf7;border:1px solid #dccfae;border-radius:18px;box-shadow:0 8px 28px #584a2920}}
.page-head{{display:flex;justify-content:space-between;align-items:flex-start;border-bottom:2px solid #b88735;padding-bottom:15px;margin-bottom:16px}}h1{{font-size:28px;margin:0 0 6px}}.page-head p{{margin:0;color:#7a6f5c;font-size:14px}}.page-no{{color:#a1742c;font-weight:700}}
.strategy-legend{{margin:0 0 16px;padding:12px 14px;background:#f8f2e5;border:1px solid #e5dcc8;border-radius:10px}}.strategy-legend h2{{font-size:14px;margin:0 0 8px;color:#785b29}}.legend-row{{display:grid;grid-template-columns:78px minmax(0,1fr);gap:9px;align-items:start;padding:5px 0;border-bottom:1px dashed #ece3cf;font-size:11.5px;line-height:1.45}}.legend-row:last-child{{border-bottom:0}}.legend-row>b{{color:#785b29;font-weight:700}}.cal{{color:#8a806e}}.cal2{{color:#a89a7e;font-size:10.5px;margin-left:6px}}
.score-table{{width:100%;border-collapse:collapse;table-layout:fixed;background:#fff;border:1px solid #e5dcc8;border-radius:10px;overflow:hidden}}.score-table th,.score-table td{{border:1px solid #e8e0d1;padding:8px 4px;text-align:center}}.score-table th{{background:#f1eadc;color:#69552f;font-size:12px;font-weight:700;white-space:nowrap}}.score-table th.stock-col,.score-table td.stock-cell{{width:190px;text-align:left}}.score-table th.strategy-col{{width:auto}}.stock-cell b{{display:block;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}.stock-cell small{{display:block;margin-top:2px;color:#8a806e;font-size:10px}}.stock-cell small.incl{{color:#a1742c;font-weight:600}}
.score-cell{{font:700 18px/1 ui-monospace,SFMono-Regular,monospace}}.score-cell.high{{color:#18734c;background:#edf7f0}}.score-cell.low{{color:#b52922;background:#fff0ee}}.score-cell.neutral{{color:#8a672b;background:#fffaf0}}.score-cell.muted{{opacity:.42;font-size:14px}}.table-empty{{padding:32px!important;color:#8a806e}}
footer{{margin-top:18px;padding-top:12px;border-top:1px solid #e5dcc8;color:#9a907f;font-size:11px;text-align:center;line-height:1.5}}
</style></head><body>{''.join(page_html)}</body></html>"""


def render_v2_signal_images(
    report: V2SignalReport, *, top_n: int = 30, list_rows: list[dict] | None = None
) -> list[bytes]:
    """Playwright 进程内渲染 HTML → 逐页截图(无需 web 路由,自包含)。"""
    from playwright.sync_api import sync_playwright

    exe = os.environ.get("STOCKFU_CHROMIUM_PATH")
    html_doc = build_v2_signal_mail_html(report, top_n=top_n, list_rows=list_rows)
    images: list[bytes] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=exe)
        try:
            page = browser.new_page(
                viewport={"width": 1360, "height": 1200}, device_scale_factor=1
            )
            page.set_content(html_doc, wait_until="domcontentloaded")
            page.wait_for_selector(".signal-page", timeout=15000)
            for el in page.query_selector_all(".signal-page"):
                images.append(el.screenshot())
        finally:
            browser.close()
    return images


def run_v2_signal_mail_job(
    as_of=None, *, top_n: int = 30, force: bool = False, send: bool = True
) -> dict:
    """单日 V2 评分 → 出图 → 发信。``send=False`` 时只出图不发(本地验证用)。

    成功标准:能生成图片邮件并发送。SMTP 未就绪/``send=False`` 时返回图片数与原因。
    """
    from datetime import date

    from stockfu.config import is_mail_ready
    from stockfu.services.quote_writer import latest_closed_trade_day

    if as_of is None:
        as_of = latest_closed_trade_day()
    if not isinstance(as_of, date):
        as_of = date.fromisoformat(str(as_of))

    from stockfu.services.v2_recommend import (
        RECOMMENDATION_ALPHA_IDS,
        _build_recommend_list,
        _rank_rows,
    )
    from stockfu.services.v2_signal import V2SignalScorer

    scorer = V2SignalScorer(alpha_ids=list(RECOMMENDATION_ALPHA_IDS))
    report = scorer.score(as_of)
    # scorer 可能把 as_of 截断到实际数据末日(交易日历预埋未来日);展示/主题一律用真实评分日。
    scored_date = report.as_of
    if report.n_scored == 0:
        return {"ok": False, "detail": "无可评分股票", "as_of": str(scored_date), "pages": 0}

    # 推荐榜单 = 综合均分前 top_n ∪ 每策略各自前 5，去重后按均分排序（与自选股荐股同规则）。
    ranked = _rank_rows(report.rows)
    list_rows = _build_recommend_list(ranked, report.alpha_ids, top_n=top_n)
    try:
        images = render_v2_signal_images(report, top_n=top_n, list_rows=list_rows)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False, "detail": f"出图失败: {type(exc).__name__}: {exc}",
            "as_of": str(scored_date), "pages": 0,
        }
    if not images:
        return {"ok": False, "detail": "未生成图片", "as_of": str(scored_date), "pages": 0}

    result: dict = {
        "as_of": str(scored_date),
        "universe_size": report.universe_size,
        "n_scored": report.n_scored,
        "pages": len(images),
        "top_n": top_n,
        "list_size": len(list_rows),
        "rule": "综合均分前 top_n ∪ 每策略各自前 5，去重后按均分排序",
    }

    if not send:
        result["ok"] = True
        result["sent"] = False
        result["detail"] = "send=False,仅出图未发信"
        return result
    if not force and not is_mail_ready():
        result["ok"] = False
        result["sent"] = False
        result["detail"] = "邮件未配置完整(账号/授权码/收件人)"
        return result

    mail = send_card_email(
        images,
        subject=f"StockFu V2 策略评分 · {scored_date.isoformat()}",
        title=f"StockFu V2 策略评分 · {scored_date.isoformat()}",
        description=(
            f"推荐榜单 {len(list_rows)} 只（综合前 {top_n} ∪ 各策略前 5） · "
            f"全宇宙 {report.universe_size} 只 · 各策略独立 0–100 分 · 50 为中性"
        ),
        filename_prefix="stockfu-v2-signal",
        include_attachments=False,
    )
    result["ok"] = bool(mail.get("ok", False))
    result["sent"] = result["ok"]
    result["mail"] = mail
    return result
