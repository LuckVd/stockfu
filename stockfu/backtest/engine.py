"""回测共享仿真基础层（V2 专用）。

V1 策略回测引擎（run_backtest 主循环 + 止盈/组合刹车/regime 门禁族）已移除，
策略配置归档见 docs/legacy/strategy-v1/；本模块保留 V2 引擎（v2_engine.py /
v2_signal.py）复用的已验证单元：

  - 费用口径:佣金/最低佣金/过户费/印花税(stamp_duty_rate 分段)常量
  - 记账撮合:VirtualAccount + Position(T+1、整百股、涨跌停约束、分红/送股入账)
  - 数据预载:_preload_market_range 列式预载、_get_day_market 单日行情、
    分红/财务预载器、settle_dividends
  - 绩效与日历:_metrics(基准交集 excess)、_trade_calendar_days(快照感知)

口径与设计见 docs/BACKTEST.md §0/§5.1。无未来函数:每个 as_of 只用 ≤as_of 数据。
"""
from __future__ import annotations

import math
from array import array
from bisect import bisect_left, bisect_right
from collections import namedtuple
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

import numpy as np
from sqlmodel import select, and_

from stockfu.db import session_scope

if TYPE_CHECKING:
    from stockfu.models import FinancialReport

INITIAL_CASH = 1_000_000.0
COMMISSION_RATE = 0.0003      # 券商佣金 万3(双边)
MIN_COMMISSION = 5.0          # 最低 5 元/笔
STAMP_DUTY_RATE = 0.0005      # 印花税 0.05%(仅卖出,现行最新;2023-08-28 起)
STAMP_DUTY_RATE_OLD = 0.001   # 印花税 0.1%(仅卖出,2023-08-28 前)
STAMP_DUTY_CUTOFF = date(2023, 8, 28)   # 印花税减半生效日(千一→万五)
TRANSFER_FEE_RATE = 0.00001   # 过户费 0.001%(双边,2022 起沪深统一)
BENCHMARK = "sh000300"        # 沪深300（回测基准，2005 起；与 cn_large_pool 大盘股池口径一致）


def stamp_duty_rate(as_of: date | None) -> float:
    """印花税率(仅卖出单边征收):2023-08-28 前 0.001(千一),之后 0.0005(万五)。

    as_of=None → 现行最新 0.0005(向后兼容:无日期回退,如实盘即时成交)。
    P2-3 第一步:跨历史区间回测费用不失真(旧版全期按 0.0005,2023-08 前低估一半)。
    """
    if as_of is None or as_of >= STAMP_DUTY_CUTOFF:
        return STAMP_DUTY_RATE
    return STAMP_DUTY_RATE_OLD


@dataclass
class Position:
    shares: int = 0
    avg_cost: float = 0.0
    lots: list[tuple[int, date]] = field(default_factory=list)  # (shares, buy_date), FIFO for dividend tax
    # 已除权但尚未上市的送转股。它们计入经济权益，但在上市日前不得卖出。
    receivable_shares: int = 0
    # 持仓期间最高收盘价，供分级追踪止盈。清仓后下次买入会重置。
    peak_close: float = 0.0
    # 分段止盈锚点与已触发阶段。分段减仓后限制目标仓位，避免次日被选股逻辑买回。
    take_profit_anchor_shares: int = 0
    take_profit_fired: set[str] = field(default_factory=set)
    take_profit_cap_shares: int | None = None


class VirtualAccount:
    """虚拟账户:现金 + 持仓。借鉴 trading.recompute_holding 的移动加权平均(纯内存)。"""

    def __init__(self, initial_cash: float = INITIAL_CASH,
                 fractional_codes: set[str] | None = None):
        self.cash: float = float(initial_cash)
        # 已除息但尚未支付的现金。应收属于权益，不属于可用于买入的现金。
        self.cash_receivable: float = 0.0
        self.initial: float = float(initial_cash)
        self.positions: dict[str, Position] = {}
        self.fee_paid: float = 0.0
        self.dividend_received: float = 0.0
        self.dividend_tax_paid: float = 0.0
        # 分数仓标的(行业指数等按权重交易,无整百股约束);空集=全市场整百股。
        self.fractional_codes: set[str] = fractional_codes or set()

    def _fractional(self, code: str) -> bool:
        return code in self.fractional_codes

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + self.cash_receivable + sum(
            (p.shares + p.receivable_shares) * prices.get(c, 0.0)
            for c, p in self.positions.items() if p.shares > 0 or p.receivable_shares > 0
        )

    def weight(self, code: str, prices: dict[str, float]) -> float:
        total = self.equity(prices)
        if total <= 0:
            return 0.0
        pos = self.positions.get(code)
        if not pos or (pos.shares <= 0 and pos.receivable_shares <= 0):
            return 0.0
        return (pos.shares + pos.receivable_shares) * prices.get(code, 0.0) / total

    def apply_action(self, code: str, action: str, target_weight: float,
                     price: float, prices: dict[str, float],
                     as_of: date | None = None) -> dict | None:
        """按 target_weight 调仓(整百股)。返回交易记录(含 realized pnl)或 None。

        买入受可用现金约束(不足则收敛到能买的整百股);卖出按目标算股数。
        action 仅用于记录语义(buy/add/reduce/sell),实际方向由 target vs current 决定。
        as_of: 成交日,决定卖出印花税率(见 stamp_duty_rate);None=现行最新。
        """
        if price <= 0 or action == "hold":
            return None
        total = self.equity(prices)
        if total <= 0:
            return None
        target_value = target_weight * total
        pos = self.positions.setdefault(code, Position())
        # 应收股属于经济权益，目标权重必须看见它；但实际卖出仍只会取 settled shares。
        current_value = (pos.shares + pos.receivable_shares) * price
        delta = target_value - current_value  # 正=买,负=卖
        if abs(delta) < total * 0.001:        # 调仓量太小,不动
            return None

        if delta > 0:  # 买入
            buy_value = min(delta, self.cash)
            if self._fractional(code):
                # 分数仓(指数):按权重直接买,无整百股约束;费用后扣,与 probe NotionalAccount 一致。
                shares = buy_value / price
                if shares <= 0:
                    return None
                cost = shares * price
                fee = (max(cost * COMMISSION_RATE, MIN_COMMISSION)
                       + cost * TRANSFER_FEE_RATE)
                new_total = pos.shares + shares
                pos.avg_cost = (pos.avg_cost * pos.shares + cost) / new_total
                pos.shares = new_total
                if pos.shares == shares:
                    pos.peak_close = price
                if not pos.take_profit_fired:
                    pos.take_profit_anchor_shares = new_total
                    pos.take_profit_cap_shares = None
                pos.lots.append((shares, as_of or date.today()))
                self.cash -= (cost + fee)
                self.fee_paid += fee
                return {"kind": action, "code": code, "shares": round(shares, 4),
                        "price": price, "fee": round(fee, 2), "pnl": None}
            shares = int(buy_value / price / 100) * 100   # A 股整百股
            if shares <= 0:
                # 建仓特例:目标增量不足 100 股但现金够 1 手(+费用)时建最小仓。
                # 预检必须纳入费用 —— 旧版只判 price*100,扣 cost+fee 后 cash 会落到约 -5 元。
                est_cost = price * 100
                est_fee = max(est_cost * COMMISSION_RATE, MIN_COMMISSION) + est_cost * TRANSFER_FEE_RATE
                if pos.shares == 0 and self.cash >= est_cost + est_fee:
                    shares = 100
                else:
                    return None
            cost = shares * price
            fee = max(cost * COMMISSION_RATE, MIN_COMMISSION) + cost * TRANSFER_FEE_RATE
            new_total = pos.shares + shares
            pos.avg_cost = (pos.avg_cost * pos.shares + cost) / new_total  # 移动加权平均
            pos.shares = new_total
            # 新开仓从成交价重新计峰；加仓保留已有峰值，避免“回撤”被人为抹平。
            if pos.shares == shares:
                pos.peak_close = price
            if not pos.take_profit_fired:
                pos.take_profit_anchor_shares = new_total
                pos.take_profit_cap_shares = None
            pos.lots.append((shares, as_of or date.today()))
            self.cash -= (cost + fee)
            self.fee_paid += fee
            return {"kind": action, "code": code, "shares": shares, "price": price,
                    "fee": round(fee, 2), "pnl": None}
        else:          # 卖出
            sell_value = -delta
            if self._fractional(code):
                # 分数仓(指数):按目标权重清/减,无整百股约束。
                shares = min(sell_value / price, pos.shares)
                if shares <= 0:
                    return None
                proceeds = shares * price
                fee = (max(proceeds * COMMISSION_RATE, MIN_COMMISSION)
                       + proceeds * (stamp_duty_rate(as_of) + TRANSFER_FEE_RATE))
                realized = (price - pos.avg_cost) * shares - fee
                pos.shares -= shares
                remaining = shares
                kept: list[tuple[int, date]] = []
                for lot_shares, lot_date in pos.lots:
                    sold = min(remaining, lot_shares)
                    remaining -= sold
                    if lot_shares > sold:
                        kept.append((lot_shares - sold, lot_date))
                    elif remaining < 0:  # 防御性分支，正常不会触发
                        kept.append((-remaining, lot_date))
                        remaining = 0
                pos.lots = kept
                self.cash += (proceeds - fee)
                self.fee_paid += fee
                if pos.shares <= 1e-9:
                    pos.shares = 0
                    pos.avg_cost = 0.0
                    pos.receivable_shares = 0
                    pos.take_profit_fired = set()
                    pos.take_profit_anchor_shares = 0
                    pos.take_profit_cap_shares = None
                    pos.peak_close = 0.0
                return {"kind": action, "code": code, "shares": -round(shares, 4),
                        "price": price, "fee": round(fee, 2), "pnl": round(realized, 2)}
            shares = int(sell_value / price / 100) * 100
            shares = min(shares, pos.shares)
            if shares <= 0:
                return None
            proceeds = shares * price
            fee = (max(proceeds * COMMISSION_RATE, MIN_COMMISSION)
                   + proceeds * (stamp_duty_rate(as_of) + TRANSFER_FEE_RATE))
            realized = (price - pos.avg_cost) * shares - fee   # 已实现盈亏(扣费后,含印花税+过户费)
            pos.shares -= shares
            remaining = shares
            kept: list[tuple[int, date]] = []
            for lot_shares, lot_date in pos.lots:
                sold = min(remaining, lot_shares)
                remaining -= sold
                if lot_shares > sold:
                    kept.append((lot_shares - sold, lot_date))
                elif remaining < 0:  # 防御性分支，正常不会触发
                    kept.append((-remaining, lot_date))
                    remaining = 0
            pos.lots = kept
            self.cash += (proceeds - fee)
            self.fee_paid += fee
            if pos.shares == 0:
                pos.avg_cost = 0.0
                pos.peak_close = 0.0
                pos.take_profit_anchor_shares = 0
                pos.take_profit_fired.clear()
                pos.take_profit_cap_shares = None
            elif not pos.take_profit_fired:
                pos.take_profit_anchor_shares = pos.shares
                pos.take_profit_cap_shares = None
            return {"kind": action, "code": code, "shares": -shares, "price": price,
                    "fee": round(fee, 2), "pnl": round(realized, 2)}

    def credit_dividend(self, code: str, per_share_cash: float, as_of: date,
                        record_date: date | None = None) -> dict | None:
        """除息日为隔夜持仓入账现金分红，并按持有期扣缴红利税。"""
        pos = self.positions.get(code)
        if not pos or pos.shares <= 0 or per_share_cash <= 0:
            return None
        ref_date = record_date or as_of
        # 正常回测路径的买入都会建立 lots；兼容手工构造仓位时保守按最高税率。
        lots = pos.lots or [(pos.shares, ref_date)]
        gross = tax = 0.0
        covered = 0
        for shares, buy_date in lots:
            if shares <= 0:
                continue
            covered += shares
            amount = shares * per_share_cash
            held_days = max((ref_date - buy_date).days, 0)
            rate = 0.20 if held_days <= 30 else (0.10 if held_days <= 365 else 0.0)
            gross += amount
            tax += amount * rate
        if covered < pos.shares:
            amount = (pos.shares - covered) * per_share_cash
            gross += amount
            tax += amount * 0.20
        net = gross - tax
        self.cash += net
        self.dividend_received += gross
        self.dividend_tax_paid += tax
        return {"kind": "cash_dividend", "code": code, "shares": pos.shares,
                "gross": round(gross, 2), "tax": round(tax, 2), "net": round(net, 2),
                "per_share_cash": per_share_cash}

    def adjust_for_stock_dividend(self, code: str, per_share_stock: float,
                                  as_of: date) -> dict | None:
        """除权日前收市的持仓获送股/转增：调股数、成本及 FIFO lots，不动现金。

        ``per_share_stock`` 是每旧股新增股数（10转10=1.0）。正常 A 股整手和
        每10股方案会保持整数；仍显式 round + lot 对账，避免浮点使后续整百股卖出失真。
        调用者须在同日现金分红之后、开盘挂单之前调用。
        """
        pos = self.positions.get(code)
        if not pos or pos.shares <= 0 or per_share_stock <= 0:
            return None
        factor = 1.0 + per_share_stock
        old_shares = pos.shares
        new_shares = int(round(old_shares * factor))
        if new_shares <= old_shares:
            return None
        if pos.lots:
            lots = [(int(round(shares * factor)), buy_date)
                    for shares, buy_date in pos.lots]
            # 逐 lot round 后与总仓位的差额归到最老 lot，保持 FIFO 总和不变量。
            diff = new_shares - sum(shares for shares, _d in lots)
            shares0, date0 = lots[0]
            lots[0] = (shares0 + diff, date0)
            pos.lots = lots
        else:
            pos.lots = [(new_shares, as_of)]
        pos.shares = new_shares
        pos.avg_cost /= factor
        return {"kind": "stock_dividend", "code": code,
                "shares_before": old_shares, "shares_after": new_shares,
                "per_share_stock": per_share_stock, "factor": factor}


# =====================================================================
# 内部辅助
# =====================================================================



def _get_trade_price(code: str, open_prices: dict[str, float],
                     close_prices: dict[str, float]) -> tuple[float, str]:
    """获取成交价:open 优先,close 兜底。返回 (price, source)。"""
    px = open_prices.get(code)
    if px is not None and px > 0:
        return px, "open"
    px = close_prices.get(code)
    if px is not None and px > 0:
        return px, "close_fallback"
    return 0.0, "unavailable"


# 紧凑 bar 下标:(qfq OHLC, pct, st, status, amount, raw OHLC, pe, pb, hfq close/open)。
# 信号使用 qfq；成交现实层(涨跌停/费用/整手)用 raw；账户估值层默认 qfq(涨跌幅
# 复权,含分红再投,§0.1);hfq 仅作对账。正式口径见 docs/BACKTEST.md。
(_BI_O, _BI_H, _BI_L, _BI_C, _BI_PCT, _BI_ST, _BI_TS, _BI_AMT,
 _BI_O_RAW, _BI_H_RAW, _BI_L_RAW, _BI_C_RAW, _BI_PE, _BI_PB,
 _BI_C_HFQ, _BI_O_HFQ) = range(16)

# quote_series 字段 → 列式 array key(供回测内存供给器切片)
_QS_FIELD_KEY = {
    "open": "o", "high": "h", "low": "l", "close": "c", "close_raw": "c_raw",
    "close_hfq": "c_hfq", "open_hfq": "o_hfq",
    # 非价格字段(amount/market_cap/turnover)也走列式预载 → size/low_turnover/
    # illiquidity 等基本面点因子回测零 DB(quote_series 用同名 key 命中供给器)。
    "amount": "amt", "market_cap": "mcap", "turnover": "turn",
}

# 列式 array 的字段 key;预载时按此填充 array('d')。前 16 个对应 _BI_* 下标
# (旧 tuple 路径用);末尾 mcap/turn 为后加,仅供 size/low_turnover 等点因子读,
# 不进 _bar_from_cols 当日 bar(按名访问,顺序无关)。
_COL_KEYS = (
    "o", "h", "l", "c", "pct", "st", "ts", "amt",
    "o_raw", "h_raw", "l_raw", "c_raw", "pe", "pb",
    "c_hfq", "o_hfq",
    "mcap", "turn",
)

# 列式预载结构:series={code: {col_key: array('d', len(dates))}}(缺失=nan),
# dates=升序交易日历,date_idx={date:int} 整数索引,valid={code: array('b')}(1=当日有 SQL 行)。
# 替代旧 {date:{code:tuple}} 双层 dict —— 用全局整数索引替代 dict，降低预载内存。
_SeriesCtx = namedtuple("_SeriesCtx", ["series", "dates", "date_idx", "valid"])

def _canonical_dividend_rows(rows) -> list[tuple[str, object]]:
    """将库内事件按证券、除权日规范化，禁止重复事件在回测中双记。

    写入路径本就拒绝同证券同除权日的冲突；历史库可能已含旧重复行，故读取路径
    也必须执行同一规则。完全相同的行可安全折叠，任何金额或日期冲突立即失败，
    不能以“最后一行覆盖”伪造一个可运行的长期回测。
    """
    from stockfu.data.base import DividendEventDTO
    from stockfu.services.dividend import _canonical_events

    grouped: dict[str, list[object]] = {}
    for row in rows:
        grouped.setdefault(row.asset_code, []).append(row)
    out: list[tuple[str, object]] = []
    for code, code_rows in grouped.items():
        events = [
            DividendEventDTO(
                ex_date=row.ex_date,
                per_share_cash=float(row.per_share_cash or 0.0),
                per_share_stock=float(row.per_share_stock or 0.0),
                record_date=row.record_date,
                announce_date=row.announce_date,
                currency=row.currency or "CNY",
                source=row.source or "db:dividend_event",
            )
            for row in code_rows
        ]
        for event in _canonical_events(events):
            out.append((code, event))
    return sorted(out, key=lambda item: (item[0], item[1].ex_date))


def _load_canonical_dividend_rows(codes: list[str], start: date, end: date) -> list[tuple[str, object]]:
    """读取回测窗口事件，并在进入任何账户/因子路径前完成规范化。"""
    from stockfu.models import DividendEvent

    if not codes:
        return []
    with session_scope() as s:
        rows = s.exec(select(DividendEvent).where(
            DividendEvent.asset_code.in_(codes),
            DividendEvent.ex_date >= start,
            DividendEvent.ex_date <= end,
        ).order_by(DividendEvent.asset_code, DividendEvent.ex_date)).all()
    return _canonical_dividend_rows(rows)


def _preload_dividend_events(codes: list[str], start: date, end: date) -> dict[str, list[tuple[date, float | None]]]:
    """一次 SQL 预载回测宇宙的分红事件，供 TTM 股息率按日切片。"""
    out: dict[str, list[tuple[date, float | None]]] = {code: [] for code in codes}
    for code, event in _load_canonical_dividend_rows(codes, start, end):
        out.setdefault(code, []).append((event.ex_date, event.per_share_cash))
    return out


def _preload_financial_reports(codes: list[str], end: date) -> dict[str, list["FinancialReport"]]:
    """一次 SQL 预载回测宇宙的财务三表（profit+balance+cashflow，pub_date <= end）。

    供质量因子（quality_roe/gpoa/net_margin/cash_quality/leverage/asset_growth）
    按日字段级 PIT 过滤，零逐票查库。不限制 start：ROE 稳定性等需要多年历史年报，
    每 code 三表约 200 行，全量可接受。code 无财务数据 → 空列表（区别于未预载）。
    """
    from sqlmodel import select

    from stockfu.services.financial import _rows_to_reports
    from stockfu.models import FinancialBalance, FinancialCashflow, FinancialProfit

    out: dict[str, list] = {code: [] for code in codes}
    rows = []
    with session_scope() as s:
        rows += s.exec(select(FinancialProfit).where(
            FinancialProfit.asset_code.in_(codes),
            FinancialProfit.pub_date <= end)).all()
        rows += s.exec(select(FinancialBalance).where(
            FinancialBalance.asset_code.in_(codes),
            FinancialBalance.pub_date <= end)).all()
        rows += s.exec(select(FinancialCashflow).where(
            FinancialCashflow.asset_code.in_(codes),
            FinancialCashflow.pub_date <= end)).all()
    by_code: dict[str, list] = {}
    for r in rows:
        by_code.setdefault(r.asset_code, []).append(r)
    for code, rws in by_code.items():
        out[code] = _rows_to_reports(rws)
    return out


def _preload_cash_dividends(codes: list[str], start: date, end: date) -> dict[date, list[tuple[str, float, date | None]]]:
    """预载除息日现金流；与因子用 TTM 索引分开，避免改变其供给接口。"""
    out: dict[date, list[tuple[str, float, date | None]]] = {}
    for code, event in _load_canonical_dividend_rows(codes, start, end):
        cash = float(event.per_share_cash or 0.0)
        if cash > 0:
            out.setdefault(event.ex_date, []).append((code, cash, event.record_date))
    return out


def _preload_stock_dividends(codes: list[str], start: date, end: date) -> dict[date, list[tuple[str, float]]]:
    """预载除权日送股/转增，供账户结算；与现金流分开以强制现金先、送转后。"""
    out: dict[date, list[tuple[str, float]]] = {}
    for code, event in _load_canonical_dividend_rows(codes, start, end):
        stock = float(event.per_share_stock or 0)
        if stock > 0:
            out.setdefault(event.ex_date, []).append((code, stock))
    return out


def settle_dividends(
    acct: VirtualAccount, as_of: date,
    cash_dividends: dict[date, list[tuple[str, float, date]]],
    stock_dividends: dict[date, list[tuple[str, float]]],
    credit_dividends: bool,
) -> list[dict]:
    """公司行为结算(研究模式 non-strict 主线):仅 raw 口径。返回新增 trade 记录。

    qfq/hfq 三复权价已含现金分红再投+送转,credit_dividends=False 时两者全跳过
    (再入账/调仓=重复计息)。raw 下顺序:先除息日现金分红入账(扣红利税),
    后除权日送转调股数(不动现金)。抽出为纯函数便于 hermetic 单测门控行为。
    """
    records: list[dict] = []
    if not credit_dividends:
        return records
    for code, cash, record_date in cash_dividends.get(as_of, []):
        rec = acct.credit_dividend(code, cash, as_of, record_date)
        if rec:
            rec.update(date=as_of.isoformat(), status="credited")
            records.append(rec)
    for code, stock in stock_dividends.get(as_of, []):
        rec = acct.adjust_for_stock_dividend(code, stock, as_of)
        if rec:
            rec.update(date=as_of.isoformat(), status="credited")
            records.append(rec)
    return records



@contextmanager
def _backtest_series_ctx(
    sctx: _SeriesCtx | None,
    dividend_index: dict[str, list[tuple[date, float | None]]] | None = None,
    financial_index: dict[str, list["FinancialReport"]] | None = None,
):
    """挂载 factors.quote_series 的内存供给器:从列式预载 sctx 切片,零 DB。

    sctx 为列式结构(series/dates/date_idx/valid)。provide 用 bisect 在全局 dates 上
    切窗口 [start, ref_date],从对应 array 取值,nan 过滤(等价旧 None 过滤)。
    与 DB quote_series 逐值一致:同一行集、同窗口、同升序、同缺失过滤(窗口左溢出时
    两者都返回库内最早日起的部分序列,行为相同)。code/字段不在预载 → 返回 None 回落查库
    (保正确)。结束自动摘除 → live 路径与未预载调用方不受影响。
    """
    from stockfu.services.factors import (clear_backtest_bars_provider,
                                          clear_backtest_series_provider,
                                          set_backtest_bars_provider,
                                          set_backtest_series_provider)
    from stockfu.services.dividend import (clear_backtest_dividend_provider,
                                           set_backtest_dividend_provider)
    from stockfu.services.valuation import (
        _ValWindow,
        clear_backtest_valuation_provider,
        set_backtest_valuation_provider,
    )
    from stockfu.services.financial import (
        clear_backtest_financial_provider,
        set_backtest_financial_provider,
    )
    if not sctx or not sctx.series:
        yield
        return

    series, dates, _date_idx, _valid = sctx
    dividend_index = dividend_index or {}

    def provide(code, field, start, ref_date):
        cols = series.get(code)
        if cols is None:
            return None                       # code 不在预载宇宙 → 回落
        key = _QS_FIELD_KEY.get(field)
        if key is None:
            return None                       # 未知字段 → 回落
        arr = cols[key]
        lo = bisect_left(dates, start)
        hi = bisect_right(dates, ref_date)
        return [v for v in arr[lo:hi] if not math.isnan(v)]

    def provide_bars(code, field, start, ref_date):
        """同 provide 但同时返回日期(供 monthly/weekly_bollinger 按日聚合;零额外查库)。"""
        cols = series.get(code)
        if cols is None:
            return None
        key = _QS_FIELD_KEY.get(field)
        if key is None:
            return None
        arr = cols[key]
        lo = bisect_left(dates, start)
        hi = bisect_right(dates, ref_date)
        d_out: list = []
        v_out: list = []
        for i in range(lo, hi):
            v = arr[i]
            if not math.isnan(v):
                d_out.append(dates[i])
                v_out.append(v)
        return d_out, v_out

    def provide_valuation(code, start, ref_date):
        """返回 PE/PB 估值原生窗口(_ValWindow),供 value 算子零 DB 算历史分位。

        向量化:array('d') 切片零拷贝 + numpy 检测 ETF/指数全 nan 回退,跳过旧逐行
        建 tuple 循环。输出经 valuation_snapshot 的 numpy 过滤/排序路径,逐值等价旧
        tuple 路径(test_valuation_equivalence 盯)。
        """
        cols = series.get(code)
        if cols is None:
            return None                       # code 不在预载宇宙 → 回落 DB
        lo = bisect_left(dates, start)
        hi = bisect_right(dates, ref_date)
        if hi <= lo:
            return _ValWindow(array("d"), array("d"), array("d"), 0)  # 空窗 → snapshot 走 empty
        pe_win = cols["pe"][lo:hi]            # array('d') 切片 → array('d')(非 list)
        pb_win = cols["pb"][lo:hi]
        c_win = cols["c"][lo:hi]
        # ETF/指数预载行没有 PE/PB(全 nan);valuation_snapshot 原路径只查
        # QuoteSnapshot,故此处回退 DB,避免把非个股 bar 误当估值样本。
        pe_np = np.frombuffer(pe_win, dtype=np.float64)
        pb_np = np.frombuffer(pb_win, dtype=np.float64)
        if np.all(np.isnan(pe_np) & np.isnan(pb_np)):
            return None                       # 全 nan → 回退 DB(等价旧 any_pe_pb=False)
        return _ValWindow(pe_win, pb_win, c_win, hi - lo - 1)

    def provide_dividends(code, start, ref_date):
        events = dividend_index.get(code)
        if events is None:
            return None
        return [
            (ex_date, cash) for ex_date, cash in events
            if start <= ex_date <= ref_date
        ]

    def provide_financial(code, ref_date):
        """返回该股票全部财报（(year, quarter) 降序，预载已按 pub_date<=end 过滤）。

        字段级 PIT 由 services.financial.FinancialReport.visible 在因子层判定
        （三表公告日可能不同，不能在此统一按最早公告日切片）；code 不在预载
        宇宙 → None 回落 DB。
        """
        return financial_index.get(code)

    set_backtest_series_provider(provide)
    set_backtest_bars_provider(provide_bars)
    set_backtest_valuation_provider(provide_valuation)
    set_backtest_dividend_provider(provide_dividends)
    if financial_index is not None:
        set_backtest_financial_provider(provide_financial)
    try:
        yield
    finally:
        clear_backtest_series_provider()
        clear_backtest_bars_provider()
        clear_backtest_valuation_provider()
        clear_backtest_dividend_provider()
        if financial_index is not None:
            clear_backtest_financial_provider()


def _pack_bar_row(r) -> tuple:
    """ORM 行 → 定长 tuple(区间预载用;比 dict 省数倍)。

    成交价优先显式前复权 *_qfq,回落遗留 open/high/low/close。
    """
    def _fq(primary: str, legacy: str):
        v = getattr(r, primary, None)
        if v is None:
            v = getattr(r, legacy, None)
        return float(v) if v is not None else None

    def _f(name):
        v = getattr(r, name, None)
        return float(v) if v is not None else None
    is_st = getattr(r, "is_st", None)
    trade_status = getattr(r, "trade_status", None)
    return (
        _fq("open_qfq", "open"), _fq("high_qfq", "high"),
        _fq("low_qfq", "low"), _fq("close_qfq", "close"), _f("pct_chg"),
        1 if is_st else 0,
        int(trade_status) if trade_status is not None else 1,
        _f("amount"),
        _f("open_raw"), _f("high_raw"), _f("low_raw"), _f("close_raw"),
        _f("pe"), _f("pb"),
        _f("close_hfq"), _f("open_hfq"),
    )


def _bar_from_tuple(t: tuple) -> dict:
    """紧凑 tuple → 旧 day_bars 字段 dict(调用方字段名不变)。"""
    return {
        "open": t[_BI_O], "high": t[_BI_H], "low": t[_BI_L], "close": t[_BI_C],
        "open_raw": t[_BI_O_RAW], "high_raw": t[_BI_H_RAW],
        "low_raw": t[_BI_L_RAW], "close_raw": t[_BI_C_RAW],
        "pct_chg": t[_BI_PCT],
        "is_st": bool(t[_BI_ST]),
        "trade_status": int(t[_BI_TS]) if t[_BI_TS] is not None else 1,
        "amount": t[_BI_AMT],
        "close_hfq": t[_BI_C_HFQ], "open_hfq": t[_BI_O_HFQ],
    }


def _bar_from_cols(cols: dict, di: int) -> dict:
    """列式当日切片 → bar dict(nan→None);字段名/语义与 _bar_from_tuple 完全一致。

    供列式预载路径的 _get_day_market 用;出口已把 nan 还原 None,下游的
    ``a or b`` coalesce 与 ``is not None`` 判断无需改动。
    """
    def _f(k):
        v = cols[k][di]
        return None if math.isnan(v) else v
    return {
        "open": _f("o"), "high": _f("h"), "low": _f("l"), "close": _f("c"),
        "open_raw": _f("o_raw"), "high_raw": _f("h_raw"),
        "low_raw": _f("l_raw"), "close_raw": _f("c_raw"),
        "pct_chg": _f("pct"),
        "is_st": bool(cols["st"][di]),         # st 预载填 0.0/1.0,永不为 nan
        "trade_status": int(cols["ts"][di]),   # ts 预载填 1.0,永不为 nan
        "amount": _f("amt"),
        "close_hfq": _f("c_hfq"), "open_hfq": _f("o_hfq"),
    }



def _bar_from_row(r) -> dict:
    """ORM 行情行 → 日 bar dict(字段缺失时用 getattr 默认,兼容 ETF/指数表无 is_st 等列)。"""
    return _bar_from_tuple(_pack_bar_row(r))



def _preload_market_range(codes: list[str], start: date, end: date) -> _SeriesCtx | None:
    """区间 raw SQL 列式预载行情 → _SeriesCtx(series, dates, date_idx, valid)。

    全局交易日历 dates(升序)+ date_idx(date→int);每个 code 的 14 字段各一个
    array('d')(按 dates 对齐,缺失=nan)+ valid array('b')(1=当日有 SQL 行)。
    不经 ORM 全量物化(峰值过高);只 SELECT 必要列 + fetchmany,按 quote_model_for 分表。
    两遍扫描:第一遍只收集全局 date + code 集合(不存行,省下 ~1.5G 临时 rows 峰值),
    建日历后预分配 array;第二遍重查填值。列式 + 整数索引替代旧 {date:{code:tuple}}
    双层 dict,内存从 ~3.4G 降到 ~0.9G(19 年窗口)。
    """
    from sqlalchemy import text

    from stockfu.db import read_engine
    from stockfu.services.factors import quote_model_for
    db_engine = read_engine()

    if not codes:
        return None
    # 表名: SQLModel/SQLAlchemy __tablename__
    groups: dict[str, list[str]] = {}
    for c in codes:
        model = quote_model_for(c)
        groups.setdefault(model.__tablename__, []).append(c)

    # 各表列略有差异:统一取共有 OHLC + 可选字段
    # quote_snapshot 有 is_st/trade_status/amount/pct_chg; etf/index 可能缺 is_st
    # 个股:COALESCE 显式前复权列与遗留列(迁移过渡期两者可能只填一侧)
    col_sets = {
        "quote_snapshot": (
            "asset_code, quote_date, "
            "COALESCE(open_qfq, open), COALESCE(high_qfq, high), "
            "COALESCE(low_qfq, low), COALESCE(close_qfq, close), pct_chg, "
            "is_st, trade_status, amount, open_raw, high_raw, low_raw, close_raw, pe, pb, "
            "close_hfq, open_hfq, market_cap, turnover"
        ),
        "etf_quote_daily": (
            "asset_code, quote_date, open, high, low, close, pct_chg, "
            "NULL as is_st, 1 as trade_status, amount, NULL as open_raw, NULL as high_raw, NULL as low_raw, NULL as close_raw, NULL as pe, NULL as pb, "
            "NULL as close_hfq, NULL as open_hfq, NULL as market_cap, NULL as turnover"
        ),
        "index_quote_daily": (
            "asset_code, quote_date, open, high, low, close, pct_chg, "
            "NULL as is_st, 1 as trade_status, amount, NULL as open_raw, NULL as high_raw, NULL as low_raw, NULL as close_raw, NULL as pe, NULL as pb, "
            "NULL as close_hfq, NULL as open_hfq, NULL as market_cap, NULL as turnover"
        ),
    }
    start_s = start.isoformat()
    end_s = end.isoformat()

    def _chunks(xs, n=400):
        for i in range(0, len(xs), n):
            yield xs[i:i + n]

    def _run_query(conn):
        """生成器:依次 yield 各分表分块的 result(两遍扫描共用 SQL 文本)。"""
        for table, cs in groups.items():
            cols = col_sets.get(table)
            if not cols:
                continue
            for chunk in _chunks(cs, 400):
                ph = ", ".join(f":c{i}" for i in range(len(chunk)))
                params = {f"c{i}": v for i, v in enumerate(chunk)}
                params["start"] = start_s
                params["end"] = end_s
                sql = text(
                    f"SELECT {cols} FROM {table} "
                    f"WHERE quote_date >= :start AND quote_date <= :end "
                    f"AND asset_code IN ({ph})"
                )
                yield conn.execute(sql, params)

    NAN = float("nan")
    # 第一遍:收集全局交易日 + code 集合(不存行,避免 770 万行临时 list 的内存峰值)
    all_dates: set = set()
    codes_seen: set = set()
    with db_engine.connect() as conn:
        for result in _run_query(conn):
            while True:
                batch = result.fetchmany(5000)
                if not batch:
                    break
                for row in batch:
                    asset_code, qdate = row[0], row[1]
                    if isinstance(qdate, str):
                        qdate = date.fromisoformat(qdate[:10])
                    all_dates.add(qdate)
                    codes_seen.add(asset_code)
    if not all_dates:
        return None

    g_dates = sorted(all_dates)
    g_date_idx = {d: i for i, d in enumerate(g_dates)}
    n = len(g_dates)
    # 预分配列式 array:per code 14 字段 array('d')(全 nan)+ valid array('b')(全 0)
    series: dict[str, dict[str, array]] = {
        code: {k: array("d", [NAN] * n) for k in _COL_KEYS}
        for code in codes_seen
    }
    valid: dict[str, array] = {code: array("b", [0] * n) for code in codes_seen}
    # 第二遍:重查填值
    with db_engine.connect() as conn:
        for result in _run_query(conn):
            while True:
                batch = result.fetchmany(5000)
                if not batch:
                    break
                for row in batch:
                    (asset_code, qdate, o, h, l, c, pct,
                     is_st, trade_status, amount, o_raw, h_raw, l_raw, close_raw, pe, pb,
                     close_hfq, open_hfq, market_cap, turnover) = row
                    if isinstance(qdate, str):
                        qdate = date.fromisoformat(qdate[:10])
                    di = g_date_idx[qdate]
                    colsd = series[asset_code]
                    colsd["o"][di] = float(o) if o is not None else NAN
                    colsd["h"][di] = float(h) if h is not None else NAN
                    colsd["l"][di] = float(l) if l is not None else NAN
                    colsd["c"][di] = float(c) if c is not None else NAN
                    colsd["pct"][di] = float(pct) if pct is not None else NAN
                    colsd["st"][di] = 1.0 if is_st else 0.0
                    colsd["ts"][di] = float(int(trade_status) if trade_status is not None else 1)
                    colsd["amt"][di] = float(amount) if amount is not None else NAN
                    colsd["o_raw"][di] = float(o_raw) if o_raw is not None else NAN
                    colsd["h_raw"][di] = float(h_raw) if h_raw is not None else NAN
                    colsd["l_raw"][di] = float(l_raw) if l_raw is not None else NAN
                    colsd["c_raw"][di] = float(close_raw) if close_raw is not None else NAN
                    colsd["pe"][di] = float(pe) if pe is not None else NAN
                    colsd["pb"][di] = float(pb) if pb is not None else NAN
                    colsd["c_hfq"][di] = float(close_hfq) if close_hfq is not None else NAN
                    colsd["o_hfq"][di] = float(open_hfq) if open_hfq is not None else NAN
                    colsd["mcap"][di] = float(market_cap) if market_cap is not None else NAN
                    colsd["turn"][di] = float(turnover) if turnover is not None else NAN
                    valid[asset_code][di] = 1
    return _SeriesCtx(series=series, dates=g_dates, date_idx=g_date_idx, valid=valid)


def _pick_px(bar: dict, hfq_key: str, raw_key: str, qfq_key: str,
             basis: str) -> float | None:
    """按估值口径选价:hfq→后复权(回落 raw→qfq);qfq→前复权(回落 raw);raw→raw(回落 qfq)。

    显式 ``is not None`` 判断(_bar_from_cols 出口已 nan→None;不用 ``or`` 以免
    对 0.0/极端值误判)。day_bars 内的 raw OHLC 不经此函数,check_fill 永远吃 raw。
    qfq 为研究模式(§0.3)收益主线:已含分红再投,故 credit_dividends 关闭。
    """
    if basis == "hfq":
        for k in (hfq_key, raw_key, qfq_key):
            v = bar.get(k)
            if v is not None:
                return v
        return None
    if basis == "qfq":
        v = bar.get(qfq_key)
        if v is not None:
            return v
        return bar.get(raw_key)   # 极少:qfq 未回补时回落 raw(优于 None)
    v = bar.get(raw_key)
    return v if v is not None else bar.get(qfq_key)


def _get_day_market(codes: list[str], as_of: date,
                    sctx: _SeriesCtx | None = None,
                    valuation_basis: str = "qfq",
                    ) -> tuple[dict[str, float], dict[str, float], dict[str, dict]]:
    """单日行情 → (close_prices, open_prices, day_bars)。

    sctx(列式预载)命中则零 SQL;否则按表分组一次 SELECT(兼容未预载/单测)。
    close_prices/open_prices 按估值口径选价(hfq=后复权总收益,raw=不复权);
    day_bars 内 raw/qfq/hfq 齐全 → 信号用 qfq、check_fill 用 raw、估值用 hfq/raw。
    """
    if sctx is not None and sctx.series:
        di = sctx.date_idx.get(as_of)
        if di is None:
            return {}, {}, {}
        series, _dates, _date_idx, valid = sctx
        close_prices: dict[str, float] = {}
        open_prices: dict[str, float] = {}
        day_bars: dict[str, dict] = {}
        for code in codes:
            vb = valid.get(code)
            if vb is None or not vb[di]:
                continue                  # 当日无 SQL 行(停牌/未上市/退市)→ 跳过
            bar = _bar_from_cols(series[code], di)
            day_bars[code] = bar
            cv = _pick_px(bar, "close_hfq", "close_raw", "close", valuation_basis)
            if cv is not None:
                close_prices[code] = cv
            ov = _pick_px(bar, "open_hfq", "open_raw", "open", valuation_basis)
            if ov is not None:
                open_prices[code] = ov
        return close_prices, open_prices, day_bars
    # DB 回落路径(未预载/单测):沿用 ORM + _bar_from_row,语义与列式路径一致。
    from stockfu.services.factors import quote_model_for
    if not codes:
        return {}, {}, {}
    groups: dict[type, list[str]] = {}
    for c in codes:
        groups.setdefault(quote_model_for(c), []).append(c)
    close_prices: dict[str, float] = {}
    open_prices: dict[str, float] = {}
    day_bars: dict[str, dict] = {}
    with session_scope() as s:
        for model, cs in groups.items():
            rows = s.exec(
                select(model).where(
                    and_(model.quote_date == as_of, model.asset_code.in_(cs))
                )
            ).all()
            for r in rows:
                bar = _bar_from_row(r)
                day_bars[r.asset_code] = bar
                cv = _pick_px(bar, "close_hfq", "close_raw", "close", valuation_basis)
                if cv is not None:
                    close_prices[r.asset_code] = cv
                ov = _pick_px(bar, "open_hfq", "open_raw", "open", valuation_basis)
                if ov is not None:
                    open_prices[r.asset_code] = ov
    return close_prices, open_prices, day_bars



# 绩效计算
# =====================================================================


def _metrics(equity_curve: list[dict], benchmark: list[dict],
             initial: float, days: int,
             bench_window: dict | None = None) -> dict:
    """算绩效:总收益/年化/最大回撤/夏普/胜率(基准对比)。

    bench_window: {"start","end"} 基准实际可用窗口。excess 按交集算:
    取 equity_curve 在基准窗口内的子段,与该窗口的 benchmark_return 对比。
    """
    import math

    eq = [p["equity"] for p in equity_curve]
    bm = [p["equity"] for p in benchmark] if benchmark else []
    out: dict = {}

    total_r = None
    if eq and initial > 0:
        total_r = (eq[-1] / initial - 1) * 100
        out["total_return"] = round(total_r, 2)
        if days > 0 and eq[-1] > 0:
            out["annualized"] = round(((eq[-1] / initial) ** (252 / days) - 1) * 100, 2)
        peak, max_dd = eq[0], 0.0
        last_peak_idx = 0
        max_dd_peak_idx = 0
        max_dd_trough_idx = 0
        # 本金水下计数：相对初始资金，而非相对运行中的历史峰值。
        # 字段名沿用 schema-2，避免破坏下游读取；gt0 为低于本金，geN 为亏损至少 N%。
        u0 = u10 = u20 = u30 = 0
        for i, v in enumerate(eq):
            if v > peak:
                peak = v
                last_peak_idx = i
            if peak > 0:
                dd = (peak - v) / peak
                if dd > max_dd:
                    max_dd = dd
                    max_dd_peak_idx = last_peak_idx
                    max_dd_trough_idx = i
            principal_loss_pct = (initial - v) / initial * 100
            if principal_loss_pct > 0:
                u0 += 1
            if principal_loss_pct >= 10:
                u10 += 1
            if principal_loss_pct >= 20:
                u20 += 1
            if principal_loss_pct >= 30:
                u30 += 1
        out["max_drawdown"] = round(max_dd * 100, 2)
        # 回本:最大回撤谷底 → 净值收回回撤前峰值(peak_val)的交易日数;未回本=None。
        # 本金水下分布:权益低于初始资金 / 相对初始资金亏损至少 10/20/30% 的交易日占比。
        peak_val = eq[max_dd_peak_idx]
        rec_idx = next(
            (j for j in range(max_dd_trough_idx, len(eq)) if eq[j] >= peak_val),
            None,
        )
        out["max_drawdown_recovered"] = rec_idx is not None
        out["max_drawdown_recovery_days"] = (
            rec_idx - max_dd_trough_idx if rec_idx is not None else None
        )
        _n_eq = len(eq) or 1
        out["underwater_basis"] = "initial_principal"
        out["underwater_days_gt0"] = u0
        out["underwater_days_ge10"] = u10
        out["underwater_days_ge20"] = u20
        out["underwater_days_ge30"] = u30
        out["underwater_pct_gt0"] = round(u0 / _n_eq * 100, 1)
        out["underwater_pct_ge10"] = round(u10 / _n_eq * 100, 1)
        out["underwater_pct_ge20"] = round(u20 / _n_eq * 100, 1)
        out["underwater_pct_ge30"] = round(u30 / _n_eq * 100, 1)
        rets = [(eq[i] / eq[i - 1] - 1) for i in range(1, len(eq)) if eq[i - 1] > 0]
        if len(rets) >= 5:
            mean = sum(rets) / len(rets)
            std = (sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5
            out["sharpe"] = round(mean / std * math.sqrt(252), 2) if std > 0 else 0.0
            # sortino:仅用下行波动(负收益),衡量"坏波动"风险调整收益
            downside = [r for r in rets if r < 0]
            if len(downside) >= 2:
                dstd = (sum(r * r for r in downside) / (len(downside) - 1)) ** 0.5
                out["sortino"] = round(mean / dstd * math.sqrt(252), 2) if dstd > 0 else 0.0
            else:
                out["sortino"] = None
            # calmar:年化收益 / 最大回撤(风险调整,越大越好)
            mdd = out.get("max_drawdown")
            if out.get("annualized") is not None and mdd and mdd > 0:
                out["calmar"] = round(out["annualized"] / mdd, 2)
        else:
            out["sharpe"] = None
            out["sortino"] = None

    # 基准:按交集窗口算 excess(2026-08-24 修复:此前用 total_return(全窗口) -
    # benchmark_return(基准自身窗口)直接相减,基准首点晚于回测起点时分子分母
    # 窗口错位 → excess 系统性偏差)。交集语义:策略收益从基准窗口首日当日的
    # equity 起算,与基准同窗对比;基准覆盖全窗口时基期=首日 equity。
    out["benchmark_window"] = bench_window
    if bm and bm[0] > 0:
        out["benchmark_return"] = round((bm[-1] / bm[0] - 1) * 100, 2)
        strat_ret = total_r
        if bench_window and equity_curve:
            bstart = str(bench_window.get("start") or "")
            if bstart:
                base_pt = next(
                    (p for p in equity_curve
                     if str(p.get("date", "")) >= bstart),
                    None,
                )
                if base_pt is not None and base_pt["equity"] > 0:
                    strat_ret = (eq[-1] / base_pt["equity"] - 1) * 100
        out["excess"] = round(
            (strat_ret if strat_ret is not None else 0.0)
            - out["benchmark_return"], 2)
    else:
        out["benchmark_return"] = None
        out["excess"] = None
        out["benchmark_reason"] = "无数据（index_quote_daily 无对应区间数据）"

    return out



def _trade_calendar_days(start: date, end: date) -> list[date]:
    from stockfu.db import has_read_engine_override
    # V2 快照隔离（阻塞①最大漏点）：快照激活时，交易日历必须来自快照 quote_snapshot，
    # 不能走 akshare 联网——否则改主库/断网重跑会产生不同日历，破坏可复现性。
    if not has_read_engine_override():
        from stockfu.services.snapshot import _trade_calendar
        cal = _trade_calendar() or []
        if cal:
            return sorted(d for d in cal if start <= d <= end)
    # 快照激活，或 akshare 不可用：用 quote_snapshot 历史行情日构造
    # （session_scope 跟随 read_engine，快照激活时读快照）。
    from sqlmodel import select
    from stockfu.db import session_scope
    from stockfu.models import QuoteSnapshot
    with session_scope() as s:
        cal = {d for d in s.exec(select(QuoteSnapshot.quote_date).distinct()).all() if d}
    return sorted(d for d in cal if start <= d <= end)


# =====================================================================