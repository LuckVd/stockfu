"""策略评分推荐卡片：HTML 组装、Playwright 截图与 SMTP 发送。"""
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


def _strategy_html(strategy: dict) -> str:
    factors = strategy.get("factors") or {}
    ranked = sorted(
        ((key, value) for key, value in factors.items() if value is not None),
        key=lambda item: -abs(float(item[1])) if isinstance(item[1], (int, float)) else 0,
    )[:3]
    chips = "".join(
        f"<span>{_esc(key)} {_score(value)}</span>" for key, value in ranked
    )
    error = strategy.get("error")
    if error:
        chips = f"<span class='err'>{_esc(error)}</span>"
    score = strategy.get("score")
    return (
        "<div class='strategy'>"
        f"<div class='strategy-name'>{_esc(strategy.get('strategy_name') or strategy.get('strategy_id'))}</div>"
        f"<div class='strategy-score {_score_class(score)}'>{_score(score)}</div>"
        f"<div class='factor-chips'>{chips}</div>"
        "</div>"
    )


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


def _stock_html(row: dict) -> str:
    strategies = ""
    if row.get("factor_mail_enabled"):
        strategies = "".join(_strategy_html(item) for item in (row.get("strategies") or []))
        if not strategies:
            strategies = "<div class='empty'>本次无因子评分</div>"
    llm = _llm_html(row.get("llm")) if row.get("llm_enabled") else ""
    return (
        "<section class='stock'>"
        f"<header><div><b>{_esc(row.get('name') or row.get('code'))}</b>"
        f"<small>{_esc(row.get('code'))}</small></div>"
        f"<span>{'因子 ' if row.get('factor_mail_enabled') else ''}"
        f"{'LLM' if row.get('llm_enabled') else ''}</span></header>"
        f"{strategies}{llm}</section>"
    )


def build_signal_mail_html(report: dict) -> str:
    rows = report.get("rows") or []
    pages = list(_chunks(rows, 5)) or [[]]
    page_html: list[str] = []
    for page_no, page_rows in enumerate(pages, 1):
        body = "".join(_stock_html(row) for row in page_rows)
        if not body:
            body = "<div class='page-empty'>尚未选择接收因子评分或 LLM 分析的股票。</div>"
        page_html.append(
            "<main class='signal-page'>"
            "<div class='page-head'>"
            "<div><h1>StockFu 策略评分</h1>"
            f"<p>{_esc(report.get('signal_date') or '暂无批次')} · "
            f"每个策略独立 0–100 分，50 为中性</p></div>"
            f"<div class='page-no'>{page_no}/{len(pages)}</div></div>"
            f"{body}"
            "<footer>仅供研究参考，不构成投资建议；评分不读取实际持仓，也不承诺收益。</footer>"
            "</main>"
        )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>StockFu 策略评分</title>
<style>
*{{box-sizing:border-box}}html,body{{margin:0;background:#f4f0e6;color:#27231d;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif}}
body{{padding:24px}}.signal-page{{width:820px;min-height:1160px;margin:0 auto 24px;padding:34px 38px;background:#fffdf7;border:1px solid #dccfae;border-radius:18px;box-shadow:0 8px 28px #584a2920}}
.page-head{{display:flex;justify-content:space-between;align-items:flex-start;border-bottom:2px solid #b88735;padding-bottom:18px;margin-bottom:18px}}h1{{font-size:28px;margin:0 0 6px}}.page-head p{{margin:0;color:#7a6f5c;font-size:14px}}.page-no{{color:#a1742c;font-weight:700}}
.stock{{border:1px solid #e5dcc8;border-radius:12px;margin:0 0 14px;overflow:hidden;background:#fff}}.stock>header{{display:flex;justify-content:space-between;align-items:center;background:#f8f2e5;padding:11px 14px}}.stock>header b{{font-size:17px}}.stock>header small{{margin-left:8px;color:#8a806e}}.stock>header>span{{font-size:12px;color:#8a672b}}
.strategy{{display:grid;grid-template-columns:1fr 72px 2fr;gap:12px;align-items:center;padding:10px 14px;border-top:1px solid #eee7d8}}.strategy-name{{font-size:13px;font-weight:650}}.strategy-score{{font:700 24px/1 ui-monospace,SFMono-Regular,monospace;text-align:right}}.high{{color:#b52922}}.low{{color:#18734c}}.neutral{{color:#8a672b}}.factor-chips{{display:flex;gap:5px;flex-wrap:wrap}}.factor-chips span{{font-size:10px;padding:3px 6px;border-radius:9px;background:#f2eee5;color:#6f6657}}
.llm{{margin:10px 14px 13px;padding:10px 12px;border-left:3px solid #8269b2;background:#f6f2fb;border-radius:5px;font-size:12px}}.llm-head{{display:flex;align-items:center;gap:10px}}.llm-head span{{font:700 21px ui-monospace,SFMono-Regular,monospace}}.llm-head small{{margin-left:auto;color:#82758e}}.llm p{{margin:7px 0}}.llm-cols{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}.llm ul{{margin:0;padding-left:17px}}.llm .risks{{color:#8a4e36}}.empty{{color:#8a806e;padding:12px 14px}}.err{{color:#a43c32}}.page-empty{{padding:180px 20px;text-align:center;color:#8a806e}}footer{{margin-top:18px;padding-top:12px;border-top:1px solid #e5dcc8;color:#9a907f;font-size:11px;text-align:center}}
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
            page = browser.new_page(viewport={"width": 960, "height": 1320}, device_scale_factor=2)
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
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
        return {"ok": False, "detail": f"推荐卡片出图失败: {type(exc).__name__}: {exc}", "pages": 0}
    if not images:
        return {"ok": False, "detail": "未生成任何推荐卡片", "pages": 0}
    signal_date = report.get("signal_date") or ""
    return send_card_email(
        images,
        subject=f"StockFu 策略评分 · {signal_date}",
        title=f"StockFu 策略评分 · {signal_date}",
        description=f"{len(rows)} 只股票 · 各策略独立 0–100 分 · 50 为中性",
        filename_prefix="stockfu-signal",
    )
