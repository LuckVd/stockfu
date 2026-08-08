"""策略评分推荐邮件：长表 HTML 组装、Playwright 截图与 SMTP 发送。"""
from __future__ import annotations

import html
import os
from itertools import islice
from typing import Iterable
from urllib.parse import urlencode

from stockfu.services.mail import DEFAULT_BASE_URL, send_card_email


def _chunks(items: list, size: int) -> Iterable[list]:
    it = iter(items)
    while chunk := list(islice(it, size)):
        yield chunk


def _esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _score(value) -> str:
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return "—"


def _score_class(value) -> str:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return "neutral"
    if score >= 60:
        return "high"
    if score <= 40:
        return "low"
    return "neutral"


# The aliases keep ten strategy columns readable in a mail image.  The
# strategy name shown in the legend still comes from the database/report when
# available; these names and one-line descriptions are safe fallbacks for an
# empty report or a newly added strategy.
SIGNAL_STRATEGY_BRIEFS: dict[str, tuple[str, str, str]] = {
    "dividend_cross_section_partial_exposure_brake_regime_trend_take_profit": (
        "红利趋稳",
        "红利横截面·趋势刹车",
        "红利因子叠加趋势状态的组合敞口刹车。",
    ),
    "dividend_cross_section_partial_exposure_brake_take_profit#deep": (
        "红利深刹",
        "红利横截面·深度敞口刹车",
        "红利横截面选股并施加更深的组合敞口刹车。",
    ),
    "small_cap_low_turnover": (
        "小盘低换",
        "小盘低换手",
        "小市值代理因子与低换手因子的组合。",
    ),
    "low_turnover_reversal": (
        "低换反转",
        "低换手反转",
        "低换手、短期反转与价值信号的组合。",
    ),
    "graham_defensive_value": (
        "格雷厄姆",
        "格雷厄姆防御价值",
        "PE/PB、股息与低波因子构成的防御价值组合。",
    ),
    "low_beta_dividend": (
        "低β红利",
        "低贝塔红利",
        "低贝塔、股息率与价值信号的组合。",
    ),
    "anti_lottery_defensive": (
        "反彩票",
        "反彩票防御",
        "规避高 MAX，同时偏向低波与价值。",
    ),
    "illiquidity_value": (
        "流动价值",
        "非流动性价值",
        "Amihud 非流动性、价值与反转信号的组合。",
    ),
    "smart_beta_multi_factor": (
        "SmartBeta",
        "智能贝塔多因子",
        "52 周高、低波、价值、低换手与 Graham 的复合参照。",
    ),
    "low_skewness": (
        "低偏度",
        "低偏度防御",
        "用低收益偏度构建的防彩票防御信号。",
    ),
}


def _strategy_fallback(strategy_id: str) -> tuple[str, str, str]:
    """Return a readable alias/name/brief for any strategy id."""
    if strategy_id in SIGNAL_STRATEGY_BRIEFS:
        return SIGNAL_STRATEGY_BRIEFS[strategy_id]
    readable = strategy_id.replace("_", " ") if strategy_id else "策略"
    return readable[:6], readable, "该策略的独立 0–100 因子评分。"


def _strategy_meta(report: dict) -> list[dict]:
    """Collect visible strategies in report order for legend/table columns.

    A strategy that is present only on an unsubscribed row must not leak into
    the email legend.  Requested ids are therefore filtered by ids observed on
    at least one factor-mail-enabled row; the row metadata supplies the full
    database name where possible.
    """
    rows = report.get("rows") or []
    observed: dict[str, str] = {}
    for row in rows:
        if not row.get("factor_mail_enabled"):
            continue
        for strategy in row.get("strategies") or []:
            strategy_id = str(strategy.get("strategy_id") or "").strip()
            if strategy_id and strategy_id not in observed:
                observed[strategy_id] = str(strategy.get("strategy_name") or "").strip()

    requested = [str(item).strip() for item in (report.get("strategy_ids") or []) if str(item).strip()]
    ordered_ids: list[str] = []
    for strategy_id in requested + list(observed):
        if strategy_id in observed or not requested:
            if strategy_id not in ordered_ids:
                ordered_ids.append(strategy_id)

    result: list[dict] = []
    for strategy_id in ordered_ids:
        alias, fallback_name, brief = _strategy_fallback(strategy_id)
        result.append(
            {
                "strategy_id": strategy_id,
                "short_name": alias,
                "full_name": observed.get(strategy_id) or fallback_name,
                "brief": brief,
            }
        )
    return result


def _llm_html(llm: dict | None) -> str:
    if not llm:
        return "<div class='llm empty'>LLM：本次无结果</div>"
    if llm.get("status") != "success":
        return f"<div class='llm err'>LLM 失败：{_esc(llm.get('error'))}</div>"
    reasons = "".join(f"<li>{_esc(item)}</li>" for item in (llm.get("reasons") or [])[:3])
    risks = "".join(f"<li>{_esc(item)}</li>" for item in (llm.get("risks") or [])[:2])
    return (
        "<div class='llm'>"
        f"<div class='llm-head'><b>LLM 独立评分</b>"
        f"<span class='{_score_class(llm.get('score'))}'>{_score(llm.get('score'))}</span>"
        f"<small>{_esc(llm.get('model'))}</small></div>"
        f"<p>{_esc(llm.get('summary'))}</p>"
        f"<div class='llm-cols'><ul>{reasons}</ul><ul class='risks'>{risks}</ul></div>"
        "</div>"
    )


def _score_cell(strategy: dict | None, *, title: str = "") -> str:
    if not strategy:
        return "<td class='score-cell neutral'>—</td>"
    error = strategy.get("error")
    cell_title = title or str(error or "")
    title_attr = f" title='{_esc(cell_title)}'" if cell_title else ""
    return (
        f"<td class='score-cell {_score_class(strategy.get('score'))}'{title_attr}>"
        f"{_score(strategy.get('score'))}</td>"
    )


def _table_html(rows: list[dict], strategies: list[dict], show_llm: bool) -> str:
    head = "".join(
        f"<th class='strategy-col' title='{_esc(item['full_name'])}'>"
        f"{_esc(item['short_name'])}</th>"
        for item in strategies
    )
    if show_llm:
        head += "<th class='llm-col'>LLM</th>"
    body: list[str] = []
    for row in rows:
        by_id = {
            str(item.get("strategy_id")): item
            for item in (row.get("strategies") or [])
            if item.get("strategy_id")
        }
        stock_name = _esc(row.get("name") or row.get("code") or "—")
        stock_code = _esc(row.get("code") or "")
        cells = [
            f"<td class='stock-cell'><b>{stock_name}</b><small>{stock_code}</small></td>"
        ]
        for item in strategies:
            strategy = by_id.get(item["strategy_id"]) if row.get("factor_mail_enabled") else None
            cells.append(_score_cell(strategy, title=item["full_name"]))
        if show_llm:
            llm = row.get("llm") if row.get("llm_enabled") else None
            if llm and llm.get("status") == "success":
                cells.append(_score_cell(llm, title="LLM 独立评分"))
            elif llm and llm.get("status"):
                cells.append(f"<td class='score-cell neutral' title='{_esc(llm.get('error'))}'>失败</td>")
            else:
                cells.append("<td class='score-cell neutral'>—</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    if not body:
        colspan = 1 + len(strategies) + (1 if show_llm else 0)
        body.append(f"<tr><td class='table-empty' colspan='{colspan}'>本页没有可展示的股票。</td></tr>")
    return (
        "<table class='score-table'><thead><tr>"
        "<th class='stock-col'>股票</th>"
        f"{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"
    )


def _legend_html(strategies: list[dict]) -> str:
    if not strategies:
        return ""
    rows = "".join(
        "<div class='legend-row'>"
        f"<b>{_esc(item['short_name'])}</b>"
        f"<span><strong>{_esc(item['full_name'])}</strong>：{_esc(item['brief'])}</span>"
        "</div>"
        for item in strategies
    )
    return f"<section class='strategy-legend'><h2>策略简介（表头简称）</h2>{rows}</section>"


def _llm_notes_html(rows: list[dict]) -> str:
    notes: list[str] = []
    for row in rows:
        if not row.get("llm_enabled"):
            continue
        label = f"{row.get('name') or row.get('code') or '股票'}（{row.get('code') or ''}）"
        notes.append(f"<section class='llm-note'><h3>{_esc(label)}</h3>{_llm_html(row.get('llm'))}</section>")
    return "".join(notes)


def build_signal_mail_html(report: dict) -> str:
    rows = report.get("rows") or []
    strategies = _strategy_meta(report)
    show_llm = any(row.get("llm_enabled") for row in rows)
    # Ten short strategy columns fit comfortably at this width.  Keeping the
    # page row count bounded makes the screenshot readable in mail clients.
    pages = list(_chunks(rows, 12)) or [[]]
    page_html: list[str] = []
    for page_no, page_rows in enumerate(pages, 1):
        legend = _legend_html(strategies) if page_no == 1 else ""
        table = _table_html(page_rows, strategies, show_llm)
        llm_notes = _llm_notes_html(page_rows)
        page_html.append(
            "<main class='signal-page'>"
            "<div class='page-head'>"
            "<div><h1>StockFu 策略评分</h1>"
            f"<p>{_esc(report.get('signal_date') or '暂无批次')} · "
            f"{_esc(len(rows))} 只股票 · 每个策略独立 0–100 分，50 为中性</p></div>"
            f"<div class='page-no'>{page_no}/{len(pages)}</div></div>"
            f"{legend}{table}{llm_notes}"
            "<footer>仅供研究参考，不构成投资建议；评分不读取实际持仓，也不承诺收益。</footer>"
            "</main>"
        )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>StockFu 策略评分</title>
<style>
*{{box-sizing:border-box}}html,body{{margin:0;background:#f4f0e6;color:#27231d;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif}}
body{{padding:24px}}.signal-page{{width:1280px;min-height:900px;margin:0 auto 24px;padding:28px 30px;background:#fffdf7;border:1px solid #dccfae;border-radius:18px;box-shadow:0 8px 28px #584a2920}}
.page-head{{display:flex;justify-content:space-between;align-items:flex-start;border-bottom:2px solid #b88735;padding-bottom:15px;margin-bottom:16px}}h1{{font-size:28px;margin:0 0 6px}}.page-head p{{margin:0;color:#7a6f5c;font-size:14px}}.page-no{{color:#a1742c;font-weight:700}}
.strategy-legend{{margin:0 0 16px;padding:11px 14px;background:#f8f2e5;border:1px solid #e5dcc8;border-radius:10px}}.strategy-legend h2{{font-size:14px;margin:0 0 7px;color:#785b29}}.legend-row{{display:grid;grid-template-columns:92px minmax(0,1fr);gap:9px;align-items:center;min-height:23px;font-size:11px;line-height:1.35;white-space:nowrap;overflow:hidden}}.legend-row>b{{color:#785b29}}.legend-row>span{{overflow:hidden;text-overflow:ellipsis}}
.score-table{{width:100%;border-collapse:collapse;table-layout:fixed;background:#fff;border:1px solid #e5dcc8;border-radius:10px;overflow:hidden}}.score-table th,.score-table td{{border:1px solid #e8e0d1;padding:8px 6px;text-align:center}}.score-table th{{background:#f1eadc;color:#69552f;font-size:12px;font-weight:700;white-space:nowrap}}.score-table th.stock-col,.score-table td.stock-cell{{width:190px;text-align:left}}.score-table th.strategy-col{{width:96px}}.score-table th.llm-col{{width:86px}}.stock-cell b{{display:block;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}.stock-cell small{{display:block;margin-top:2px;color:#8a806e;font-size:10px}}.score-cell{{font:700 18px/1 ui-monospace,SFMono-Regular,monospace}}.score-cell.high{{color:#18734c;background:#edf7f0}}.score-cell.low{{color:#b52922;background:#fff0ee}}.score-cell.neutral{{color:#8a672b;background:#fffaf0}}.table-empty{{padding:32px!important;color:#8a806e}}
.llm-note{{margin-top:13px}}.llm-note h3{{font-size:13px;margin:0 0 5px;color:#69552f}}.llm{{margin:0;padding:10px 12px;border-left:3px solid #8269b2;background:#f6f2fb;border-radius:5px;font-size:12px}}.llm-head{{display:flex;align-items:center;gap:10px}}.llm-head span{{font:700 21px ui-monospace,SFMono-Regular,monospace}}.llm-head .high{{color:#b52922}}.llm-head .low{{color:#18734c}}.llm-head small{{margin-left:auto;color:#82758e}}.llm p{{margin:7px 0}}.llm-cols{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}.llm ul{{margin:0;padding-left:17px}}.llm .risks{{color:#8a4e36}}.empty{{color:#8a806e;padding:12px 14px}}.err{{color:#a43c32}}footer{{margin-top:18px;padding-top:12px;border-top:1px solid #e5dcc8;color:#9a907f;font-size:11px;text-align:center}}
</style></head><body>{''.join(page_html)}</body></html>"""


def render_signal_images(
    *,
    run_id: int | None = None,
    base_url: str = DEFAULT_BASE_URL,
    executable_path: str | None = None,
) -> list[bytes]:
    from playwright.sync_api import sync_playwright

    query = urlencode({"run_id": run_id}) if run_id is not None else ""
    url = f"{base_url}/signals/mail-view" + (f"?{query}" if query else "")
    exe = executable_path or os.environ.get("STOCKFU_CHROMIUM_PATH")
    images: list[bytes] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=exe)
        try:
            page = browser.new_page(viewport={"width": 1360, "height": 1200}, device_scale_factor=1)
            # The long-running API may not have been restarted after a code
            # update and can consequently return 404 for /signals/mail-view.
            # Fall back to the exact same report/template in-process so a
            # scheduled mail is not blocked by a stale web worker.
            rendered = False
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
                if response is not None and response.ok:
                    page.wait_for_selector(".signal-page", timeout=15000)
                    rendered = True
            except Exception:  # noqa: BLE001
                rendered = False
            if not rendered:
                from stockfu.services.signal_scan import signal_report

                page.set_content(
                    build_signal_mail_html(
                        signal_report(run_id=run_id, subscribed_only=True)
                    ),
                    wait_until="domcontentloaded",
                )
                page.wait_for_selector(".signal-page", timeout=15000)
            for element in page.query_selector_all(".signal-page"):
                images.append(element.screenshot())
        finally:
            browser.close()
    return images


def run_signal_mail_job(run_id: int | None = None, *, force: bool = False) -> dict:
    from stockfu.config import get_signal_mail_enabled, is_mail_ready
    from stockfu.services.signal_scan import signal_report

    if not force and not get_signal_mail_enabled():
        return {"ok": False, "detail": "策略推荐邮件未启用"}
    if not is_mail_ready():
        return {"ok": False, "detail": "邮件未配置完整（账号 / 授权码 / 收件人）"}
    report = signal_report(run_id=run_id, subscribed_only=True)
    rows = report.get("rows") or []
    if not rows:
        return {"ok": False, "detail": "没有开启因子邮件或 LLM 的股票，已跳过发送", "pages": 0}
    try:
        images = render_signal_images(run_id=report.get("run_id"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"推荐长表出图失败: {type(exc).__name__}: {exc}", "pages": 0}
    if not images:
        return {"ok": False, "detail": "未生成任何推荐长表", "pages": 0}
    signal_date = report.get("signal_date") or ""
    return send_card_email(
        images,
        subject=f"StockFu 策略评分 · {signal_date}",
        title=f"StockFu 策略评分 · {signal_date}",
        description=f"{len(rows)} 只股票 · 各策略独立 0–100 分 · 50 为中性",
        filename_prefix="stockfu-signal",
        include_attachments=False,
    )
