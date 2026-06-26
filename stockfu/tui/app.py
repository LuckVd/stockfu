"""textual TUI 主看板：持仓总览 + 市场情绪(fear/greed/heat) + 交易录入。

快捷键：r 刷新，b 买入，s 卖出，q 退出。
顶部汇总显示 组合盈亏/股息率 + 市场层 恐慌/贪婪/热度。
"""
from __future__ import annotations

from sqlmodel import select

from textual import work
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Footer, Header, Static

from stockfu.db import session_scope
from stockfu.models import IndexSnapshot
from stockfu.services.portfolio import PortfolioSummary, get_portfolio
from stockfu.tui.trade_screen import TradeScreen

COLS = ["代码", "名称", "持仓", "成本", "现价", "市值",
        "盈亏%", "股息率%", "年红利", "回本(年)", "币种",
        "恐慌", "贪婪", "热度"]


def _market_indices() -> dict:
    """读取市场层最新 fear/greed/heat（来自 composite 每日落库）。"""
    out: dict[str, float] = {}
    with session_scope() as s:
        for k in ("fear", "greed", "heat"):
            r = s.exec(select(IndexSnapshot).where(
                IndexSnapshot.level == "market", IndexSnapshot.scope == "MARKET",
                IndexSnapshot.index_key == k
            ).order_by(IndexSnapshot.snap_date.desc())).first()
            if r:
                out[k] = r.value
    return out


class StockFuApp(App):
    TITLE = "StockFu · 资产管理终端"
    CSS = """
    #summary { padding: 0 1; background: $boost; color: $text; }
    DataTable { margin: 0; }
    #trade_form { padding: 1 2; border: round $accent; width: 64; height: auto; }
    #tf_msg { color: $warning; }
    """

    BINDINGS = [
        ("r", "refresh", "刷新"),
        ("b", "buy", "买入"),
        ("s", "sell", "卖出"),
        ("q", "quit", "退出"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("加载中…", id="summary")
        yield DataTable(id="holdings")
        yield Footer()

    def on_mount(self) -> None:
        self.title = self.TITLE
        self.query_one(DataTable).add_columns(*COLS)
        self.load_portfolio()

    @work(thread=True, exclusive=True)
    def load_portfolio(self) -> None:
        try:
            summary = get_portfolio()
            mkt = _market_indices()
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self._render_error, str(exc))
            return
        self.call_from_thread(self._render, summary, mkt)

    def _render(self, p: PortfolioSummary, mkt: dict) -> None:
        self.query_one("#summary", Static).update(self._summary_text(p, mkt))
        dt = self.query_one(DataTable)
        dt.clear()
        for pos in p.positions:
            yld = f"{pos.ttm_yield_pct:.2f}" if pos.ttm_yield_pct is not None else "-"
            pb = f"{pos.payback_years:.1f}" if pos.payback_years else "-"
            dt.add_row(
                pos.code, (pos.name or "")[:14], f"{pos.shares:g}",
                f"{pos.avg_cost:g}", f"{pos.price:.2f}", f"{pos.market_value:.0f}",
                f"{pos.profit_pct:+.1f}", yld, f"{pos.annual_dividend:.0f}",
                pb, pos.currency,
                self._band(pos.fear), self._band(pos.greed), self._band(pos.heat),
            )

    def _render_error(self, msg: str) -> None:
        self.query_one("#summary", Static).update(f"[red]加载失败：{msg}[/red]")

    @staticmethod
    def _band(v):
        """情绪强度分档（fear/greed/heat 通用）：值越高=该情绪越强(红/警示)，越低=越弱(绿/平静)。

        fear/greed/heat 是三个各自 0-100 的独立情绪强度，不是单一恐-贪连续谱，
        故统一按"强度"标注(极强/强/中/弱/极弱)，避免"贪婪列显示极恐"之类的语义错位。
        """
        if v is None:
            return "—"
        if v >= 75:
            return f"[bold red]{v:.0f}(极强)[/bold red]"
        if v >= 55:
            return f"[red]{v:.0f}(强)[/red]"
        if v >= 45:
            return f"[dim]{v:.0f}(中)[/dim]"
        if v >= 25:
            return f"[green]{v:.0f}(弱)[/green]"
        return f"[bold green]{v:.0f}(极弱)[/bold green]"

    def _summary_text(self, p: PortfolioSummary, mkt: dict) -> str:
        mix = "  [yellow](混合币种，未换算)[/yellow]" if p.mixed_currency else ""
        return (
            f"[b]组合[/b]  市值 [b]{p.total_value:,.0f}[/b]"
            f"  |  成本 {p.total_cost:,.0f}"
            f"  |  盈亏 [b]{p.total_profit:+,.0f}[/b]"
            f"  |  整体股息率 [green]{p.blended_yield_pct:.2f}%[/green]"
            f"  |  年红利≈ {p.annual_dividend_income:,.0f}"
            f"  ||  市场 恐慌{self._band(mkt.get('fear'))}"
            f"  贪婪{self._band(mkt.get('greed'))}"
            f"  热度{self._band(mkt.get('heat'))}"
            f"  |  {p.as_of}{mix}"
        )

    def action_refresh(self) -> None:
        self.query_one("#summary", Static).update("[dim]刷新中…[/dim]")
        self.load_portfolio()

    # ---- 交易录入 ----
    def action_buy(self) -> None:
        self.push_screen(TradeScreen("buy"), self._after_trade)

    def action_sell(self) -> None:
        self.push_screen(TradeScreen("sell"), self._after_trade)

    def _notify(self, msg: str) -> None:
        """把消息更新到顶部 summary 行。"""
        self.query_one("#summary", Static).update(msg)

    @staticmethod
    def _has_stock_index(code: str) -> bool:
        """该股是否已有 stock 层情绪指数（没有则需要跑）。"""
        with session_scope() as s:
            return s.exec(select(IndexSnapshot).where(
                IndexSnapshot.level == "stock",
                IndexSnapshot.scope == code).limit(1)).first() is not None

    def _optimistic_upsert(self, code: str, shares: float, avg_cost: float,
                           price: float) -> None:
        """交易成功后乐观占位：持仓行立即显示(现价用交易价)，行情/指数异步补。

        防止"行情抓取慢 → 持仓行迟迟不出现 → 用户以为没成功又下单"的重复操作。
        load_portfolio 完成后 _render 会 clear+用真实数据重建，覆盖此占位行。
        """
        if shares <= 0:
            return  # 卖出清仓，不占位
        try:
            dt = self.query_one(DataTable)
            try:
                dt.remove_row(code)  # 去掉同 code 旧行
            except Exception:  # noqa: BLE001
                pass
            mv = price * shares
            profit_pct = ((price - avg_cost) / avg_cost * 100) if avg_cost else 0.0
            dt.add_row(
                code, "[dim](刷新中…)[/dim]", f"{shares:g}", f"{avg_cost:g}",
                f"{price:.2f}", f"{mv:.0f}", f"{profit_pct:+.1f}",
                "-", "0", "-", "",  # 股息率/年红利/回本/币种 暂占位
                self._band(None), self._band(None), self._band(None),  # 指数未算
                key=code,
            )
        except Exception:  # noqa: BLE001  占位失败不影响主流程
            pass

    def _after_trade(self, result) -> None:
        if not result:
            return
        code = result.get("code")
        shares = result.get("shares") or 0
        avg_cost = result.get("avg_cost") or 0
        price = result.get("price") or avg_cost
        # 1) 醒目 toast：明确告知成功，避免用户误判而重复下单
        self.notify(f"✓ 已记录 {code}：持仓 {shares:g}股 成本{avg_cost:g}", timeout=10)
        # 2) 乐观占位：持仓行立即显示(现价用交易价)，不等几十秒行情
        self._optimistic_upsert(code, shares, avg_cost, price)
        # 3) 后台抓真实行情 + 必要时算个股情绪指数，完成后覆盖占位
        need = bool(code) and not self._has_stock_index(code)
        if need:
            self._notify(f"[dim]{code} 后台补历史 + 算个股情绪指数中（约几十秒）…[/dim]")
            self._compute_stock_index(code)
        self.load_portfolio()

    @work(thread=True)
    def _compute_stock_index(self, code: str) -> None:
        """后台为单只个股补历史 K 线 + 抓行情 + 算三层情绪指数，完成后刷新看板。"""
        try:
            from stockfu.scheduler.jobs import ensure_stock_data_and_index
            r = ensure_stock_data_and_index(code, 1825)
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self._notify, f"[red]{code} 指数计算失败：{exc}[/red]")
            return
        self.call_from_thread(
            self._notify,
            f"[green]✓ {code} 个股情绪指数已就绪：[/green]"
            f"恐慌{self._band(r.get('fear'))} 贪婪{self._band(r.get('greed'))} "
            f"热度{self._band(r.get('heat'))}")
        self.call_from_thread(self.load_portfolio)
