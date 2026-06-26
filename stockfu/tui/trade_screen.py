"""交易录入模态屏：在 TUI 里直接买入/卖出。

按 b(买入)/s(卖出) 弹出，Tab 切换字段，回车提交。
"""
from __future__ import annotations

from datetime import datetime

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label


class TradeScreen(ModalScreen):
    BINDINGS = [("escape", "cancel", "取消")]

    def __init__(self, side: str) -> None:
        super().__init__()
        self.side = side  # "buy" / "sell"

    def compose(self) -> ComposeResult:
        title = "📈 买入" if self.side == "buy" else "📉 卖出"
        with Vertical(id="trade_form"):
            yield Label(f"[b]{title}[/b]   (Tab 切换字段，回车提交，Esc 取消)",
                        id="tf_title")
            yield Input(placeholder="代码  600519 / AAPL / HK00700", id="tf_code")
            yield Input(placeholder="股数  如 100", id="tf_shares")
            yield Input(placeholder="价格  如 1500.5", id="tf_price")
            yield Input(placeholder="日期 YYYY-MM-DD（可选，默认今天）", id="tf_date")
            yield Label("", id="tf_msg")
            yield Button("确认 (Enter)", id="tf_ok", variant="success")
            yield Button("取消 (Esc)", id="tf_cancel", variant="default")

    def _values(self):
        return (self.query_one("#tf_code", Input).value.strip(),
                self.query_one("#tf_shares", Input).value.strip(),
                self.query_one("#tf_price", Input).value.strip(),
                self.query_one("#tf_date", Input).value.strip())

    def _submit(self) -> None:
        code, shares, price, d = self._values()
        msg = self.query_one("#tf_msg", Label)
        if not (code and shares and price):
            msg.update("[red]请填齐 代码 / 股数 / 价格[/red]")
            return
        try:
            shares_f, price_f = float(shares), float(price)
        except ValueError:
            msg.update("[red]股数 / 价格必须是数字[/red]")
            return
        td = None
        if d:
            try:
                td = datetime.strptime(d, "%Y-%m-%d").date()
            except ValueError:
                msg.update("[red]日期格式应为 YYYY-MM-DD[/red]")
                return
        from stockfu.services.trading import add_transaction

        try:
            r = add_transaction(code, self.side, shares_f, price_f, td)
        except Exception as exc:  # noqa: BLE001
            msg.update(f"[red]失败：{exc}[/red]")
            return
        self.dismiss({**r, "code": code, "price": price_f})  # 带 code/price 供主屏乐观占位+算指数

    @on(Button.Pressed, "#tf_ok")
    def _on_ok(self) -> None:
        self._submit()

    @on(Button.Pressed, "#tf_cancel")
    def _on_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted)
    def _on_enter(self, event: Input.Submitted) -> None:
        self._submit()

    def action_cancel(self) -> None:
        self.dismiss(None)
