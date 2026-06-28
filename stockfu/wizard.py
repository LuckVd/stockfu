"""交互式配置向导：命令行菜单设置自选 / 抓取 / 重试 / 邮件。

用法: python main.py --config
复用 config.py 的 get_/set_ 与 trading.add_watch/remove_watch，改完直接写 db。
注意：抓取 / 邮件相关配置改后需重启 --schedule 才生效（job 是启动时挂的）。
"""
from __future__ import annotations


def _ask(prompt: str, default: str = "") -> str:
    """input，空回车返回 default。"""
    s = input(prompt).strip()
    return s if s else default


def _watchlist() -> list[tuple[str, str]]:
    from sqlmodel import select

    from stockfu.db import session_scope
    from stockfu.models import Asset
    with session_scope() as s:
        rows = s.exec(select(Asset).where(Asset.is_watch == True).order_by(Asset.code)).all()  # noqa: E712
    return [(a.code, a.name or "") for a in rows]


def show_status() -> None:
    """打印当前全部配置（自选 / 抓取 / 邮件）。"""
    from stockfu.config import (get_daily_fetch_time, get_fetch_retry_count,
                                get_fetch_retry_interval, get_mail_config)
    wl = _watchlist()
    print("\n" + "=" * 52)
    print(f"自选({len(wl)} 只): " + (
        ", ".join(f"{c}({n})" if n else c for c, n in wl) or "(空)"))
    print(f"抓取: 工作日 {get_daily_fetch_time()} | 重试 {get_fetch_retry_count()} 次 × "
          f"{get_fetch_retry_interval()} 分钟")
    m = get_mail_config()
    en = "✓启用" if m["mail_enabled"] else "✗未启用"
    acc = m["smtp_user"] or "(无账号)"
    to = m["mail_to"] or "(无收件人)"
    pwd = "已设" if m["has_password"] else "未设"
    print(f"邮件: {en} | {acc} → {to} | {m['mail_days']} {m['mail_time']} | 密码{pwd}")
    print("=" * 52)


def menu_watchlist() -> None:
    from stockfu.services import trading

    while True:
        wl = _watchlist()
        print("\n--- 自选股票(增 / 删)---")
        for i, (c, n) in enumerate(wl, 1):
            print(f"  {i:>2}. {c}  {n}")
        print("  输入代码 = 添加(如 000725 / HK00700 / AAPL) | del 代码 = 删除 | 回车 = 返回")
        cmd = input("> ").strip()
        if not cmd:
            return
        if cmd.lower().startswith("del"):
            code = cmd[3:].strip().lstrip("/").strip()
            if code:
                print("  →", trading.remove_watch(code))
        else:
            r = trading.add_watch(cmd)
            print("  →", r)
            if r.get("ok"):
                print("  (数据将在下次 --fetch / --schedule 抓取时自动补充)")


def menu_schedule() -> None:
    from stockfu.config import (get_daily_fetch_time, get_fetch_retry_count,
                                get_fetch_retry_interval, set_daily_fetch_time,
                                set_fetch_retry_count, set_fetch_retry_interval)
    print(f"\n--- 抓取时间(当前 {get_daily_fetch_time()})---")
    v = _ask("  新时间 HH:MM(回车不改): ")
    if v:
        print("  →", set_daily_fetch_time(v))
    print(f"--- 重试次数(当前 {get_fetch_retry_count()})---")
    v = _ask("  新次数(回车不改): ")
    if v:
        try:
            print("  →", set_fetch_retry_count(int(v)))
        except ValueError:
            print("  无效，已忽略")
    print(f"--- 重试间隔分钟(当前 {get_fetch_retry_interval()})---")
    v = _ask("  新间隔(回车不改): ")
    if v:
        try:
            print("  →", set_fetch_retry_interval(int(v)))
        except ValueError:
            print("  无效，已忽略")


def menu_mail() -> None:
    from stockfu.config import get_mail_config, set_mail_config

    m = get_mail_config()
    print("\n--- 邮件配置(回车 = 保留当前值)---")
    body: dict = {}
    body["smtp_user"] = _ask(f"  发件账号(当前 {m['smtp_user'] or '空'}): ", m["smtp_user"])
    body["mail_to"] = _ask(f"  收件人，多个逗号(当前 {m['mail_to'] or '空'}): ", m["mail_to"])
    body["mail_time"] = _ask(f"  发送时间 HH:MM(当前 {m['mail_time']}): ", m["mail_time"])
    body["mail_days"] = _ask(
        f"  频率 mon-fri | * | 自定义如 mon,wed,fri(当前 {m['mail_days']}): ", m["mail_days"])
    pwd = input(f"  授权码(当前{'已设，留空不改' if m['has_password'] else '空'}): ").strip()
    if pwd:
        body["smtp_pass"] = pwd
    en = _ask(f"  启用定时邮件 y/n(当前{'y' if m['mail_enabled'] else 'n'}): ",
              "y" if m["mail_enabled"] else "n")
    body["mail_enabled"] = en.lower() in ("y", "1", "yes", "true")
    set_mail_config(body)
    print("  ✓ 已保存")


def test_mail() -> None:
    from stockfu.services.mail import run_mail_job

    print("\n  生成多图 + 发送中(约 10-30 秒，需 --serve 在跑)...")
    print("  →", run_mail_job())


def run_wizard() -> None:
    from stockfu.db import init_db

    init_db()
    while True:
        show_status()
        print("\nStockFu 配置向导")
        print("  1. 自选股票(增 / 删)")
        print("  2. 抓取时间 / 重试次数 / 重试间隔")
        print("  3. 邮件(账号 / 授权码 / 收件人 / 发送时间 / 频率)")
        print("  4. 测试发送邮件(立即生成多图 + 发一封)")
        print("  0. 退出")
        c = input("选择: ").strip()
        if c == "1":
            menu_watchlist()
        elif c == "2":
            menu_schedule()
        elif c == "3":
            menu_mail()
        elif c == "4":
            test_mail()
        elif c == "0":
            print("\n提示:抓取 / 邮件配置改过后，需重启 --schedule 才生效。")
            break
