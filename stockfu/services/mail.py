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

    用 playwright 元素截图（chromium 原生渲染 → emoji 为彩色，与 webui 一致；html2canvas 会把
    部分 emoji 退化成单色简笔画）。截图前：切「暖白·琥珀」主题；隐藏分享浮层以外的一切 DOM
    （交易录入表单的价格/日期等会覆盖卡片被截进去）；视口高度 ≥ 单页 1458px，避免高元素截图
    滚动拼接时混入其他元素。

    executable_path: 默认 None（用 playwright 标准安装的 chromium）；可用环境变量
      STOCKFU_CHROMIUM_PATH 覆盖（如本地缓存版本与包不匹配时指向具体 chrome 可执行文件）。
    """
    from playwright.sync_api import sync_playwright

    exe = executable_path or os.environ.get("STOCKFU_CHROMIUM_PATH")
    imgs: list[bytes] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=exe)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 1500}, device_scale_factor=2)
            # 注入标记头：/share 据此放宽到只校验指数（邮件不渲染个股持仓页）。
            page.set_extra_http_headers({"X-Mail-Render": "1"})
            # 邮件模式跳过首页 loadAll()；截图只依赖随后 openShare() 请求的 /share。
            # 否则首页各看板的按需补数会把 ETF/个股抓取重新带进邮件链路。
            page.goto(f"{base_url}/?mail_render=1", wait_until="domcontentloaded", timeout=30000)
            # 不等主页 loadAll（慢且与本任务无关）；openShare 自取 /share
            page.wait_for_function("typeof openShare === 'function'", timeout=15000)
            page.evaluate("""async () => {
                if (typeof setTheme === 'function') setTheme('amber');     // 暖白·琥珀主题（邮件固定用此主题出图）
                await window.openShare();                                   // 取 /share + 显示弹窗
                document.querySelector('#share-mode .mode-opt[data-mode="multi"]').click();
                await new Promise((resolve, reject) => {                    // 等 .sc-page 渲染好
                    const t = setInterval(() => {
                        if (document.querySelectorAll('#share-card .sc-page').length) { clearInterval(t); resolve(); }
                    }, 50);
                    setTimeout(() => { clearInterval(t); reject(new Error('邮件分享卡片未生成页面')); }, 15000);
                });
                // 行情速览只留「大盘情绪 + 行业全景/明细」:/share 带 X-Mail-Render 头时 build_card
                // 已不返回 holdings、前端 renderShareMulti 也不生成个股持仓页。此处兜底删任何残留
                // .sc-tbl 页(防头失效等异常路径),正常路径下为 no-op(行业总览=热力网格、明细=.sc-sgrid)。
                document.querySelectorAll('#share-card .sc-page').forEach(p => {
                    if (p.querySelector('.sc-tbl')) p.remove();
                });
                // 将卡片移出遮罩层放到 body，避免 shadow-dom 合成导致底部混杂遮罩背景色
                const card = document.getElementById('share-card');
                document.body.innerHTML = '';
                document.body.appendChild(card);
                document.documentElement.style.background = '#fffdf7';
                document.body.style.background = '#fffdf7';
                document.body.style.margin = '0';
                // 去掉底部边框（否则截图底边会有一条 1px 的线）
                document.querySelectorAll('.sc-page').forEach(p => { p.style.borderBottom = '0'; });
                // emoji 强制彩色字体：卡片 .face 继承 --sans（无 emoji 字体），服务器回退到
                document.querySelectorAll('#share-card .face').forEach(el => {
                    el.style.fontFamily = '"Noto Color Emoji","Apple Color Emoji","Segoe UI Emoji"';
                });
            }""")
            for el in page.query_selector_all('#share-card .sc-page'):
                imgs.append(el.screenshot())   # 元素截图，此时卡片已在干净背景中
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
    from stockfu.services.share import export_readiness

    if not is_mail_ready():
        return {"ok": False, "detail": "邮件未配置完整（账号 / 授权码 / 收件人）"}
    readiness = export_readiness(include_watch=False)
    if not readiness["ok"]:
        return {"ok": False, "detail": "分享数据日期不完整，已跳过发信", "data": readiness}
    try:
        imgs = render_share_images()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"出图失败（确认 --serve 在跑）：{type(exc).__name__}: {exc}", "pages": 0}
    if not imgs:
        return {"ok": False, "detail": "未生成任何图片（/share 无数据？）", "pages": 0}
    return send_card_email(imgs)
