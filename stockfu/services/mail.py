"""定时邮件：用无头浏览器把分享卡片渲染成 9:16 多图 → SMTP 内嵌进一封邮件发出。

- render_share_images(): playwright 打开主页 → 调 openShare + 多图模式 → 逐页截图
  （完全复用前端渲染，图与浏览器手动导出像素级一致）
- send_card_email(): SMTP(SSL/STARTTLS) 把多图 inline 进 HTML 正文，发给收件人
- run_mail_job(): 串联两者，供 --schedule 的 mail job 与 --test-mail 复用

依赖：需 `pip install playwright && playwright install chromium`；
      运行时需 --serve 在跑（playwright 访问其页面渲染）。
"""
from __future__ import annotations

import os
import smtplib
from datetime import date
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate

DEFAULT_BASE_URL = "http://127.0.0.1:8787"


def render_share_images(base_url: str = DEFAULT_BASE_URL, executable_path: str | None = None) -> list[bytes]:
    """无头浏览器渲染分享卡片多图，返回 PNG bytes 列表（每页一张）。

    executable_path: 默认 None（用 playwright 标准安装的 chromium）；可用环境变量
      STOCKFU_CHROMIUM_PATH 覆盖（如本地缓存版本与包不匹配时指向具体 chrome 可执行文件）。
    """
    from playwright.sync_api import sync_playwright

    exe = executable_path or os.environ.get("STOCKFU_CHROMIUM_PATH")
    imgs: list[bytes] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=exe)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900}, device_scale_factor=2)
            page.goto(f"{base_url}/", wait_until="domcontentloaded", timeout=30000)
            # 不等主页 loadAll（慢且与本任务无关）；openShare 自取 /share
            page.wait_for_function("typeof openShare === 'function'", timeout=15000)
            page.evaluate("""async () => {
                await window.openShare();                                   // 取 /share + 显示弹窗
                document.querySelector('#share-mode .mode-opt[data-mode="multi"]').click();
                await new Promise(r => {                                    // 等 .sc-page 渲染好
                    const t = setInterval(() => {
                        if (document.querySelectorAll('#share-card .sc-page').length) { clearInterval(t); r(); }
                    }, 50);
                });
            }""")
            for el in page.query_selector_all('#share-card .sc-page'):
                imgs.append(el.screenshot())   # 元素截图，不受弹窗遮罩影响
        finally:
            browser.close()
    return imgs


def send_card_email(images: list[bytes], subject: str | None = None) -> dict:
    """把 images 作为 inline 图片放进一封 HTML 邮件，SMTP 发给收件人。"""
    from stockfu.config import (get_smtp_from, get_smtp_host, get_smtp_pass,
                                get_smtp_port, get_smtp_user, get_mail_to)
    from stockfu.services.snapshot import latest_trade_date

    user, pwd, to_raw = get_smtp_user(), get_smtp_pass(), get_mail_to()
    if not (user and pwd and to_raw):
        return {"ok": False, "detail": "未配置完整（账号 / 授权码 / 收件人）"}
    to_list = [t.strip() for t in to_raw.replace(";", ",").split(",") if t.strip()]
    sender = get_smtp_from() or user
    td = latest_trade_date() or date.today()
    subject = subject or f"StockFu 每日行情 · {td}"

    msg = MIMEMultipart("related")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(to_list)
    msg["Date"] = formatdate(localtime=True)

    body = [
        "<html><body style='font-family:-apple-system,sans-serif;background:#f7f8fa;padding:12px'>",
        f"<h2 style='margin:0 0 8px'>StockFu 每日行情 · {td}</h2>",
        f"<p style='color:#666;font-size:13px;margin:0 0 12px'>共 {len(images)} 张 · 9:16 竖屏分享卡片</p>",
    ]
    for i in range(len(images)):
        body.append(
            f"<div style='margin:10px 0'><img src='cid:page-{i}' "
            f"style='width:100%;max-width:420px;border:1px solid #e5e7eb;border-radius:8px;display:block'></div>"
        )
    body.append("<p style='color:#999;font-size:11px;margin-top:16px'>— 由 StockFu 自动发送 —</p></body></html>")
    msg.attach(MIMEText("".join(body), "html", "utf-8"))

    for i, img in enumerate(images):
        part = MIMEImage(img, _subtype="png")
        part.add_header("Content-ID", f"<page-{i}>")
        part.add_header("Content-Disposition", "inline", filename=f"stockfu-{td}-{i + 1}.png")
        msg.attach(part)

    host, port = get_smtp_host(), get_smtp_port()
    try:
        if port == 465:
            smtp = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            smtp = smtplib.SMTP(host, port, timeout=30)
            smtp.starttls()
        with smtp:
            smtp.login(user, pwd)
            smtp.sendmail(sender, to_list, msg.as_string())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "pages": len(images), "to": to_list, "subject": subject}


def run_mail_job() -> dict:
    """出图 + 发信，供 scheduler mail job 与 --test-mail 复用。"""
    from stockfu.config import is_mail_ready

    if not is_mail_ready():
        return {"ok": False, "detail": "邮件未配置完整（账号 / 授权码 / 收件人）"}
    try:
        imgs = render_share_images()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"出图失败（确认 --serve 在跑）：{type(exc).__name__}: {exc}", "pages": 0}
    if not imgs:
        return {"ok": False, "detail": "未生成任何图片（/share 无数据？）", "pages": 0}
    return send_card_email(imgs)
