"""回测引擎:虚拟账户 + T+1 开盘调仓 + 三层架构(信号/仓位/执行)。

执行时序(更接近真实交易):
  T 日收盘: AI 分析(基于 ≤T 数据) → 信号层输出 raw signal / ai_target_weight
  T 日盘后: 仓位层(PositionManager)目标仓位驱动 + 边沿触发 + 买入冷却
  T+1 开盘: 执行层按 T+1 开盘价调仓至目标仓位

核心设计:
  信号层  →  AI 输出 signal + 可选 ai_target_weight
  仓位层  →  compute_target_weight() 转为目标仓位
          →  PositionManager.should_act() 边沿触发+冷却
  执行层  →  VirtualAccount.apply_action() 整百股调仓

无未来函数:每个 as_of 只用 ≤as_of 数据(build_context 的 as_of 已保证)。
analyze_fn 由调用方注入(scheduler: temp=0 + prefetch 批量缓存 + 算子冷填)。
热路径:单日一次行情 SQL(_get_day_market);有 prefill 时 analyze 串行(避免线程池负优化)。
"""
from __future__ import annotations

import math
from array import array
from bisect import bisect_left, bisect_right
from collections import deque, namedtuple
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlmodel import select, and_

from stockfu.db import session_scope
from stockfu.backtest.cash_scaler import scale_buys_to_cash

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

# 资金分配 / 风控默认值；当前能力、已知偏差与目标模型统一见 docs/BACKTEST.md。
#   - 总仓安全阀留 cash sleeve,保证 Σ目标 ≤ max_gross → 执行层现金够、不夹断丢目标
#   - 规则止损补文档承诺(旧 BACKTEST.md 写"-3%止损"但代码缺失;此处参数化,A股 -3% 太敏感)
DEFAULT_MAX_GROSS = 0.90      # Σ目标权重上限(留 10% 现金;对所有 rebalancer 生效)
DEFAULT_STOP_LOSS = 0.08      # 个股成本止损:浮亏达此比例 → 强制清仓
DEFAULT_PORTFOLIO_BRAKE = 0.10  # 组合回撤刹车:equity 较峰值回撤达此值 → 全局临时降仓一半
DEFAULT_PORTFOLIO_BRAKE_SCALE = 0.50  # 组合回撤刹车触发后保留的目标仓位比例
HFQ_COVERAGE_MIN = 0.995      # hfq 口径门禁:回测窗口内有 hfq 数据的股票,close_hfq 非空率下限


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

    def __init__(self, initial_cash: float = INITIAL_CASH):
        self.cash: float = float(initial_cash)
        # 已除息但尚未支付的现金。应收属于权益，不属于可用于买入的现金。
        self.cash_receivable: float = 0.0
        self.initial: float = float(initial_cash)
        self.positions: dict[str, Position] = {}
        self.fee_paid: float = 0.0
        self.dividend_received: float = 0.0
        self.dividend_tax_paid: float = 0.0

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


def _get_quote_dict(codes: list[str], as_of: date, field: str = "close") -> dict[str, float]:
    """取单日单字段 → {code: value}，个股回测信号路径用。

    注：quote_model_for 按资产类型路由三表(G01 拆表:个股→QuoteSnapshot / ETF→EtfQuoteDaily
    / 指数→IndexQuoteDaily)，下方按 model 分组查询。个股回测信号路径只传个股 code →
    实际查 QuoteSnapshot；回测基准(_benchmark_curve)单独直读 IndexQuoteDaily，不经此函数。

    主循环优先走 _get_day_market(一次 SQL 派生 close/open/bars);本函数保留给单字段场景。
    """
    from stockfu.services.factors import quote_model_for
    groups: dict[type, list[str]] = {}
    for c in codes:
        groups.setdefault(quote_model_for(c), []).append(c)
    result: dict[str, float] = {}
    with session_scope() as s:
        for model, cs in groups.items():
            rows = s.exec(
                select(model).where(
                    and_(model.quote_date == as_of, model.asset_code.in_(cs))
                )
            ).all()
            for r in rows:
                v = getattr(r, field, None)
                if v is not None:
                    result[r.asset_code] = float(v)
    return result


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
# 信号使用 qfq；成交现实层(涨跌停/费用/整手)用 raw；账户估值层默认 hfq(总收益)，
# raw 口径下另以公司行为账本补回总回报；正式迁移计划见 docs/BACKTEST.md。
(_BI_O, _BI_H, _BI_L, _BI_C, _BI_PCT, _BI_ST, _BI_TS, _BI_AMT,
 _BI_O_RAW, _BI_H_RAW, _BI_L_RAW, _BI_C_RAW, _BI_PE, _BI_PB,
 _BI_C_HFQ, _BI_O_HFQ) = range(16)

# quote_series 字段 → 列式 array key(供回测内存供给器切片)
_QS_FIELD_KEY = {
    "open": "o", "high": "h", "low": "l", "close": "c", "close_raw": "c_raw",
    "close_hfq": "c_hfq", "open_hfq": "o_hfq",
}

# 列式 array 的 16 字段 key(顺序对应 _BI_* 下标);预载时按此填充 array('d')。
_COL_KEYS = (
    "o", "h", "l", "c", "pct", "st", "ts", "amt",
    "o_raw", "h_raw", "l_raw", "c_raw", "pe", "pb",
    "c_hfq", "o_hfq",
)

# 列式预载结构:series={code: {col_key: array('d', len(dates))}}(缺失=nan),
# dates=升序交易日历,date_idx={date:int} 整数索引,valid={code: array('b')}(1=当日有 SQL 行)。
# 替代旧 {date:{code:tuple}} 双层 dict —— 用全局整数索引替代 dict，降低预载内存。
_SeriesCtx = namedtuple("_SeriesCtx", ["series", "dates", "date_idx", "valid"])
# 回测预载需覆盖 value 的 5 年估值窗口(约 1840 历日)，并留少量边界余量。
# 这也覆盖 low_volatility 的 3 年窗口；不足时 value 会回落 DB，重新引入 N+1。
_PRELOAD_LOOKBACK_DAYS = 1900


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


# 方案A strict 账本预载(CorporateActionCoverageError + _preload_accepted_corporate_actions)
# 随 2026-07-27 研究模式反转移除:研究模式 non-strict 主线只读 dividend_event,不读仲裁账本。


def _hfq_coverage(sctx: _SeriesCtx, start: date, end: date) -> tuple[float, int, int]:
    """回测窗口 [start,end] 内,有 hfq 数据的股票的 close_hfq 非空率。

    ETF/指数/未回补 hfq 的票全程 nan → 排除出分母(它们按 raw 回落,属设计而非缺口)。
    返回 (覆盖率, 命中数, 总数)。窗口内无有效行 → (1.0, 0, 0)。
    """
    series, dates, _date_idx, valid = sctx
    lo = bisect_left(dates, start)
    hi = bisect_right(dates, end)
    if hi <= lo:
        return 1.0, 0, 0
    has_hfq = [c for c, cols in series.items()
               if any(not math.isnan(v) for v in cols["c_hfq"])]
    if not has_hfq:
        return 1.0, 0, 0
    hit = tot = 0
    for c in has_hfq:
        cols = series[c]
        vb = valid[c]
        arr = cols["c_hfq"]
        for di in range(lo, hi):
            if vb[di]:
                tot += 1
                if not math.isnan(arr[di]):
                    hit += 1
    return (hit / tot if tot else 1.0), hit, tot


@contextmanager
def _backtest_series_ctx(
    sctx: _SeriesCtx | None,
    dividend_index: dict[str, list[tuple[date, float | None]]] | None = None,
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
    from stockfu.services.valuation import (clear_backtest_valuation_provider,
                                            set_backtest_valuation_provider)
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
        """返回 PE/PB 估值窗口,供 value 算子零 DB 计算历史分位。

        close/pe/pb 出口把 nan 还原成 None(与旧 tuple 路径逐值一致),value 算子末端
        的 >0 守卫对 None/nan 同样过滤。
        """
        cols = series.get(code)
        if cols is None:
            return None
        lo = bisect_left(dates, start)
        hi = bisect_right(dates, ref_date)
        pe_arr, pb_arr, c_arr = cols["pe"], cols["pb"], cols["c"]
        out = []
        any_pe_pb = False
        for i in range(lo, hi):
            pe = pe_arr[i]
            pb = pb_arr[i]
            if not (math.isnan(pe) and math.isnan(pb)):
                any_pe_pb = True
            cv = c_arr[i]
            out.append((
                dates[i],
                None if math.isnan(cv) else cv,
                None if math.isnan(pe) else pe,
                None if math.isnan(pb) else pb,
            ))
        # ETF/指数预载行没有 PE/PB(全 nan);valuation_snapshot 原路径只查
        # QuoteSnapshot,故此处回退 DB,避免把非个股 bar 误当估值样本。
        if out and not any_pe_pb:
            return None
        return out

    def provide_dividends(code, start, ref_date):
        events = dividend_index.get(code)
        if events is None:
            return None
        return [
            (ex_date, cash) for ex_date, cash in events
            if start <= ex_date <= ref_date
        ]

    set_backtest_series_provider(provide)
    set_backtest_bars_provider(provide_bars)
    set_backtest_valuation_provider(provide_valuation)
    set_backtest_dividend_provider(provide_dividends)
    try:
        yield
    finally:
        clear_backtest_series_provider()
        clear_backtest_bars_provider()
        clear_backtest_valuation_provider()
        clear_backtest_dividend_provider()


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


def _update_atr_percent(
    bar: dict,
    previous_close: float | None,
    tr_history: deque[float],
    period: int,
) -> tuple[float | None, float | None]:
    """用复权 OHLC 更新 ATR 百分比状态,返回(本日收盘, ATR/收盘)。

    真实波幅采用复权 high/low/close,因此 raw 估值回测也不会把现金分红除息
    造成的价格跳变放大成波动。当前 bar 只使用截至 as_of 的数据,可直接用于
    T 日收盘止盈判断;period 未满时返回 None,避免用不足样本的 ATR。
    """
    close = bar.get("close")
    high = bar.get("high")
    low = bar.get("low")
    if (close is None or close <= 0 or high is None or low is None
            or high <= 0 or low <= 0 or high < low):
        return close, None
    base = previous_close if previous_close and previous_close > 0 else close
    true_range = max(
        high - low,
        abs(high - previous_close) if previous_close and previous_close > 0 else 0.0,
        abs(low - previous_close) if previous_close and previous_close > 0 else 0.0,
    )
    tr_history.append(true_range / base)
    if len(tr_history) < period:
        return close, None
    return close, sum(tr_history) / len(tr_history)


def _bar_from_row(r) -> dict:
    """ORM 行情行 → 日 bar dict(字段缺失时用 getattr 默认,兼容 ETF/指数表无 is_st 等列)。"""
    return _bar_from_tuple(_pack_bar_row(r))


def _get_day_bars(codes: list[str], as_of: date) -> dict[str, dict]:
    """单日完整 bar → {code: {open,high,low,close,pct_chg,is_st,trade_status,amount}}。

    供涨跌停近似 / 停牌 / 宇宙日 flags;仅 as_of 当日行,无未来。
    主循环优先 _get_day_market;本函数保留兼容/单测。
    """
    close_px, _open_px, bars = _get_day_market(codes, as_of)
    return bars


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

    from stockfu.db import engine as db_engine
    from stockfu.services.factors import quote_model_for

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
            "close_hfq, open_hfq"
        ),
        "etf_quote_daily": (
            "asset_code, quote_date, open, high, low, close, pct_chg, "
            "NULL as is_st, 1 as trade_status, amount, NULL as open_raw, NULL as high_raw, NULL as low_raw, NULL as close_raw, NULL as pe, NULL as pb, "
            "NULL as close_hfq, NULL as open_hfq"
        ),
        "index_quote_daily": (
            "asset_code, quote_date, open, high, low, close, pct_chg, "
            "NULL as is_st, 1 as trade_status, NULL as amount, NULL as open_raw, NULL as high_raw, NULL as low_raw, NULL as close_raw, NULL as pe, NULL as pb, "
            "NULL as close_hfq, NULL as open_hfq"
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
                     close_hfq, open_hfq) = row
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


def _preload_bench_closes(code: str, start: date, end: date) -> dict:
    """基准指数(code)在 [start, end] 的 {dates, closes}(升序、一一对应,跳过 None 收盘)。

    供大盘 regime 门禁算 MA / 已实现波动率:一次 SQL,日循环 bisect 取窗,零查库。
    """
    from stockfu.models import IndexQuoteDaily
    with session_scope() as s:
        rows = s.exec(select(IndexQuoteDaily).where(
            IndexQuoteDaily.asset_code == code,
            IndexQuoteDaily.quote_date >= start,
            IndexQuoteDaily.quote_date <= end,
        ).order_by(IndexQuoteDaily.quote_date)).all()
    dates, closes = [], []
    for r in rows:
        if r.close is None:
            continue
        dates.append(r.quote_date)
        closes.append(float(r.close))
    return {"dates": dates, "closes": closes}


def _bench_closes_asof(pre: dict, as_of: date, lookback: int = 252) -> list[float]:
    """as_of 及之前最多 lookback 根基准收盘(含 as_of);无数据 → []。"""
    dates, closes = pre.get("dates") or [], pre.get("closes") or []
    if not dates:
        return []
    i = bisect_right(dates, as_of) - 1
    if i < 0:
        return []
    lo = max(0, i - lookback + 1)
    return closes[lo:i + 1]


def _market_throttle_step(
    bench_window: list[float], *, bear_latched: bool,
    ma_days: int | None, enter_band: float, exit_band: float, bear_gross: float,
    target_vol: float | None, vol_window: int, vol_floor: float,
    max_gross: float,
) -> tuple[float, bool]:
    """大盘趋势 regime 门禁:算当日敞口 cap + 更新后的 bear_latched。

    - trend(ma_days>0):收盘跌破 N 日均线进 bear(敞口压到 bear_gross),
      涨破均线×(1+exit_band)解除;不对称带宽防 whipsaw。样本不足 → 不拦。
    - vol(target_vol>0):近 vol_window 日已实现波动率(年化)缩放,
      cap = max_gross × min(1, target_vol/realvol),下限 vol_floor。
    - 两信号都配 → 取更严 min(双门禁)。返回 (cap, new_bear_latched);cap≥max_gross=不限制。
    """
    cap = max_gross
    px = bench_window[-1] if bench_window else 0.0
    # trend:长均线滞回状态机(仿 portfolio_brake_latched,前瞻性降仓)。
    if ma_days and ma_days > 0 and len(bench_window) >= max(5, ma_days // 4) and px > 0:
        w = min(ma_days, len(bench_window))
        ma = sum(bench_window[-w:]) / w
        if not bear_latched and px < ma * (1.0 - enter_band):
            bear_latched = True
        elif bear_latched and px > ma * (1.0 + exit_band):
            bear_latched = False
        if bear_latched:
            cap = min(cap, bear_gross)
    # vol targeting:已实现波动率飙升 → 等比缩总敞口(vol cluster 危机防御)。
    if target_vol and target_vol > 0 and len(bench_window) > vol_window:
        seg = bench_window[-(vol_window + 1):]
        rets = [seg[i] / seg[i - 1] - 1.0
                for i in range(1, len(seg)) if seg[i - 1] > 0]
        if len(rets) >= max(10, vol_window // 2):
            mean_r = sum(rets) / len(rets)
            var = sum((r - mean_r) ** 2 for r in rets) / len(rets)
            realvol = (var ** 0.5) * (252.0 ** 0.5)
            if realvol > 0:
                vscale = max(min(1.0, target_vol / realvol), vol_floor)
                cap = min(cap, max_gross * vscale)
    return cap, bear_latched


def _apply_gross_cap(final: dict[str, float | None], max_gross: float) -> dict[str, float | None]:
    """总仓位安全阀:若 Σ正值权重 > max_gross,等比缩放所有正值权重到 Σ=max_gross。

    max_gross >= 1.0 或无正值 → 原样返回(不限制)。留 cash sleeve = 1 - max_gross。
    """
    if max_gross >= 1.0:
        return final
    gross = sum(w for w in final.values() if w)
    if gross <= max_gross or gross <= 0:
        return final
    factor = max_gross / gross
    return {c: (w * factor if w else w) for c, w in final.items()}


def _apply_portfolio_brake(
    final: dict[str, float | None],
    current: dict[str, float],
    meta: dict[str, dict],
    *,
    scale: float,
    mode: str,
    brake_max_gross: float | None,
    keep_ratio: float | None = None,
    add_min_score: float | None = None,
    max_weight: float = 0.15,
) -> dict[str, float | None]:
    """组合回撤期目标仓位调节(规则化风控,增强路径)。

    - mode=block_new_buys:保持旧语义(禁新买/加仓,放行减仓与风险退出)。
    - scale<1(平滑刹车):正目标 ×scale;维持(None)仓显式落为 current(不再 ×scale,
      避免长刹车期每日等比压缩把组合逐步清光),总敞口由调用方 _apply_gross_cap
      压到 brake_max_gross —— 组合级敞口真正下降(旧实现只缩正目标、维持仓 +
      cap_and_rank 每日重新填满,总敞口实际不降)。
    - scale>1(回撤加仓):只放大未满单股上限的正目标;add_min_score 设置时仅
      raw 分数≥ 阈值的票放大(质量门控)。
    - keep_ratio:刹车期只保留 raw 分数最高的 keep_ratio 比例正目标,其余清 0。
    - 结尾把显式化后的维持仓并入 brake_max_gross 总敞口(未设则交给调用方兜底)。
    """
    if mode == "block_new_buys":
        return _block_portfolio_new_buys(final, current)

    # 质量门控:只保留 raw 分数最高的 keep_ratio 比例正目标,其余清 0。
    if keep_ratio is not None and 0 < keep_ratio < 1:
        pos = [(c, w) for c, w in final.items() if w and w > 0]
        pos.sort(key=lambda x: -float((meta or {}).get(x[0], {}).get("raw") or 0.0))
        keep_n = max(1, int(len(pos) * keep_ratio))
        keep = {c for c, _ in pos[:keep_n]}
        final = {c: (w if c in keep else 0.0) for c, w in final.items()}

    if scale <= 1.0:
        # 平滑刹车:正目标 ×scale;维持仓显式落为 current(供总敞口 cap 一并压缩)。
        out: dict[str, float | None] = {}
        for c, w in final.items():
            if w is None:
                out[c] = current.get(c, 0.0)
            elif w:
                out[c] = w * scale
            else:
                out[c] = w
        final = out
    else:
        # 回撤加仓:只放大未满单股上限的正目标;可选 strong_buy 质量门控。
        out = {}
        for c, w in final.items():
            if not w or w <= 0:
                out[c] = w
                continue
            if add_min_score is not None:
                raw = (meta or {}).get(c, {}).get("raw") or 0.0
                if float(raw) < add_min_score:
                    out[c] = w  # 不放大
                    continue
            if max_weight > 0 and w < max_weight:
                out[c] = min(w * scale, max_weight)
            else:
                out[c] = w
        final = out

    # 组合级总敞口安全阀:刹车期收窄到 brake_max_gross(含被显式化的维持仓)。
    if brake_max_gross is None:
        return final
    return _apply_gross_cap(final, brake_max_gross)


def _block_portfolio_new_buys(
    final: dict[str, float | None], current: dict[str, float],
) -> dict[str, float | None]:
    """组合刹车期间禁止新建仓/加仓,但放行正常减仓与风险退出。"""
    return {
        c: (current.get(c, 0.0) if w and w > current.get(c, 0.0) else w)
        for c, w in final.items()
    }


def _take_profit_tier_parts(tier: tuple[float, ...]) -> tuple[float, float, float] | None:
    """兼容旧的二元 tier，并把卖出比例规范到(0,1]。"""
    if len(tier) < 2:
        return None
    profit = float(tier[0])
    drawdown = float(tier[1])
    sell_fraction = float(tier[2]) if len(tier) >= 3 else 1.0
    if sell_fraction <= 0:
        return None
    return profit, drawdown, min(sell_fraction, 1.0)


def _take_profit_stage(profit: float, drawdown: float) -> str:
    return f"take_profit_trailing_{profit:g}_{drawdown:g}"


def _take_profit_atr_parts(tier: tuple[float, ...]) -> tuple[float, float, float] | None:
    """解析 ATR tier=(收益门槛, ATR 倍数, 卖出比例)。"""
    if len(tier) < 2:
        return None
    profit = float(tier[0])
    multiple = float(tier[1])
    sell_fraction = float(tier[2]) if len(tier) >= 3 else 1.0
    if multiple <= 0 or sell_fraction <= 0:
        return None
    return profit, multiple, min(sell_fraction, 1.0)


def _take_profit_atr_stage(profit: float, multiple: float) -> str:
    return f"take_profit_atr_{profit:g}_{multiple:g}"


def tiered_take_profit_action(
    avg_cost: float, peak_close: float, close: float,
    tiers: tuple[tuple[float, ...], ...] = (),
    hard_profit_pct: float | None = None,
    fired_tiers: set[str] | frozenset[str] = frozenset(),
) -> tuple[str, float, str | None] | None:
    """返回应触发的分级追踪止盈原因，或 None。

    门槛和峰值都以同一持仓周期的收盘价、相对移动平均成本计算：
    先到达硬止盈收益率即卖；否则取已跨越的最高收益档，并检查当前价相对
    峰值的回撤。返回(原因, 本次卖出比例, 阶段ID)；卖出比例按首次触发前
    持仓的比例解释。日频系统无法知道日内先后顺序，故统一在收盘判断、次日
    开盘尝试执行。
    """
    if avg_cost <= 0 or peak_close <= 0 or close <= 0:
        return None
    current_profit = close / avg_cost - 1.0
    if hard_profit_pct is not None and current_profit + 1e-12 >= hard_profit_pct:
        return f"take_profit_hard_{hard_profit_pct:g}", 1.0, None
    peak_profit = peak_close / avg_cost - 1.0
    parsed = [parts for tier in tiers
              if (parts := _take_profit_tier_parts(tier)) is not None]
    for profit, drawdown, sell_fraction in sorted(parsed, reverse=True):
        stage = _take_profit_stage(profit, drawdown)
        if stage in fired_tiers:
            continue
        if (peak_profit + 1e-12 >= profit
                and close / peak_close - 1.0 <= -drawdown + 1e-12):
            return stage, sell_fraction, stage
    return None


def atr_take_profit_action(
    avg_cost: float, peak_close: float, close: float, atr_pct: float | None,
    tiers: tuple[tuple[float, ...], ...] = (),
    hard_profit_pct: float | None = None,
    fired_tiers: set[str] | frozenset[str] = frozenset(),
) -> tuple[str, float, str | None] | None:
    """按 ATR 百分比触发分级追踪止盈。

    tier 的第二个字段是 ATR 倍数,实际回撤门槛为 ``atr_pct * multiple``;
    阶段 ID 只含配置倍数而不含每日 ATR 值,避免波动率变化后重复触发同一档。
    """
    if avg_cost <= 0 or peak_close <= 0 or close <= 0:
        return None
    current_profit = close / avg_cost - 1.0
    if hard_profit_pct is not None and current_profit + 1e-12 >= hard_profit_pct:
        return f"take_profit_hard_{hard_profit_pct:g}", 1.0, None
    if atr_pct is None or atr_pct <= 0:
        return None
    peak_profit = peak_close / avg_cost - 1.0
    parsed = [parts for tier in tiers
              if (parts := _take_profit_atr_parts(tier)) is not None]
    for profit, multiple, sell_fraction in sorted(parsed, reverse=True):
        stage = _take_profit_atr_stage(profit, multiple)
        if stage in fired_tiers:
            continue
        drawdown = atr_pct * multiple
        if (peak_profit + 1e-12 >= profit
                and close / peak_close - 1.0 <= -drawdown + 1e-12):
            return stage, sell_fraction, stage
    return None


def _take_profit_remaining_fraction(
    tiers: tuple[tuple[float, ...], ...], fired_tiers: set[str] | frozenset[str],
    atr: bool = False,
) -> float:
    """根据已触发阶段计算原始持仓剩余比例。"""
    sold = 0.0
    for tier in tiers:
        parts = _take_profit_tier_parts(tier)
        if parts is None:
            continue
        profit, drawdown, sell_fraction = parts
        stage = (_take_profit_atr_stage(profit, drawdown)
                 if atr else _take_profit_stage(profit, drawdown))
        if stage in fired_tiers:
            sold += sell_fraction
    return max(0.0, 1.0 - min(sold, 1.0))


def tiered_take_profit_reason(avg_cost: float, peak_close: float, close: float,
                              tiers: tuple[tuple[float, ...], ...] = (),
                              hard_profit_pct: float | None = None) -> str | None:
    """兼容旧调用方：只返回止盈原因，不暴露分段卖出比例。"""
    action = tiered_take_profit_action(
        avg_cost, peak_close, close, tiers, hard_profit_pct,
    )
    return action[0] if action else None


# =====================================================================
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

    # 基准:按交集窗口算 excess（total_return 已在上方设置，此处只引用，不重算）
    out["benchmark_window"] = bench_window
    if bm and bm[0] > 0:
        out["benchmark_return"] = round((bm[-1] / bm[0] - 1) * 100, 2)
        out["excess"] = round((out.get("total_return") or 0.0) - out["benchmark_return"], 2)
    else:
        out["benchmark_return"] = None
        out["excess"] = None
        out["benchmark_reason"] = "无数据（index_quote_daily 无对应区间数据）"

    return out


def _benchmark_curve(code: str, days: list[date]) -> tuple[list[dict], dict | None]:
    """基准(code)在 days 上的归一化净值曲线(首日=INITIAL_CASH)，返回 (曲线, 窗口信息)。

    直读 IndexQuoteDaily（不走 quote_model_for，指数独立表）。
    窗口信息 = {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"} 或 None（无数据）。
    交集截断：早于基准首日的 days 不产出曲线点，由调用方按交集算 excess。
    """
    if not days:
        return [], None
    from stockfu.models import IndexQuoteDaily
    with session_scope() as s:
        rows = {r.quote_date: r.close for r in s.exec(
            select(IndexQuoteDaily).where(
                IndexQuoteDaily.asset_code == code,
                IndexQuoteDaily.quote_date >= min(days),
                IndexQuoteDaily.quote_date <= max(days),
            )).all() if r.close}
    if not rows:
        return [], None
    sorted_dates = sorted(rows.keys())
    window = {"start": sorted_dates[0].isoformat(), "end": sorted_dates[-1].isoformat()}
    out, last = [], None
    for d in days:
        c = rows.get(d)
        if c:
            last = c
        if last:
            out.append({"date": d.isoformat(), "equity": last})
    if out and out[0]["equity"] > 0:
        base = out[0]["equity"]
        for p in out:
            p["equity"] = round(p["equity"] / base * INITIAL_CASH, 2)
    return out, window


def _trade_calendar_days(start: date, end: date) -> list[date]:
    from stockfu.services.snapshot import _trade_calendar
    cal = _trade_calendar() or []
    if not cal:
        # fallback:akshare 交易日历不可用(离线环境)时,用 quote_snapshot 历史行情日构造
        from sqlmodel import select
        from stockfu.db import session_scope
        from stockfu.models import QuoteSnapshot
        with session_scope() as s:
            cal = {d for d in s.exec(select(QuoteSnapshot.quote_date).distinct()).all() if d}
    return sorted(d for d in cal if start <= d <= end)


# =====================================================================
# 主入口
# =====================================================================


def run_backtest(codes: list[str], start: date, end: date,
                 initial_cash: float = INITIAL_CASH, analyze_fn=None,
                 prefetch_fn=None,
                 max_workers: int = 8, buy_cool_down_days: int = 5,
                 max_target_step: float = 1.0,
                 risk_confirm_days: int = 1,
                 target_mode: str = "discrete",
                 max_weight: float = 0.15, total_dead: float = 3.0,
                 min_trade_weight: float = 0.0,
                 sell_cooldown_days: int = 0,
                 conf_gate: float = 0.0,
                 debounce=None,
                 max_gross: float = DEFAULT_MAX_GROSS,
                 stop_loss_pct: float = DEFAULT_STOP_LOSS,
                 portfolio_brake_dd: float = DEFAULT_PORTFOLIO_BRAKE,
                 portfolio_brake_scale: float = DEFAULT_PORTFOLIO_BRAKE_SCALE,
                 portfolio_brake_mode: str = "scale_all",
                 portfolio_brake_max_gross: float | None = None,
                 portfolio_brake_keep_ratio: float | None = None,
                 portfolio_brake_add_min_score: float | None = None,
                 portfolio_brake_recover_dd: float | None = None,
                 portfolio_brake_recover_high_days: int = 0,
                 portfolio_brake_tiers: tuple[tuple[float, float], ...] = (),
                 # 大盘趋势 regime 门禁(前瞻性风控,与组合回撤刹车正交 min 叠加):
                 # trend:基准收盘跌破 N 日均线 → 敞口压到 market_regime_max_gross,
                 #        涨破均线×(1+exit_band)解除(滞回防 whipsaw);
                 # vol:近 vol_window 日已实现波动率缩放(target_vol 年化)。任一配即启用。
                 market_regime_code: str | None = None,
                 market_regime_ma_days: int | None = None,
                 market_regime_enter_band: float = 0.0,
                 market_regime_exit_band: float = 0.03,
                 market_regime_max_gross: float = 0.50,
                 market_regime_target_vol: float | None = None,
                 market_regime_vol_window: int = 63,
                 market_regime_vol_floor: float = 0.30,
                 take_profit_tiers: tuple[tuple[float, ...], ...] = (),
                 take_profit_hard_pct: float | None = None,
                 take_profit_atr_period: int | None = None,
                 take_profit_atr_tiers: tuple[tuple[float, ...], ...] = (),
                 take_profit_atr_lagged: bool = False,
                 universe_rules=None,
                 execution_rules=None,
                 valuation_basis: str = "qfq") -> dict:
    """回测主循环:T+1开盘执行 + 三层架构(信号→仓位→执行)。

    每个交易日 as_of 内:
      1. 执行前日 AI 挂单(以 as_of 开盘价)
      2. 用 as_of 收盘数据跑 AI → 计算目标仓位
      3. 仓位层(PositionManager)边沿触发+买入冷却 → 挂起,次日开盘执行

    analyze_fn(code, as_of, holding_override[, cache_prefill]) 默认用 ai.analyze;
    scheduler 注入带 temp=0/断点续跑缓存的版本。prefetch_fn(codes, as_of) 可选:Phase 2
    前单日批量预读+冷 miss 填充 → 注入 analyze 的 cache_prefill(跳过逐次 get/save
    往返);为 None 时退回原路径。analyze_fn 须能接 cache_prefill(第 4 参)才用预读。

    去抖旋钮(默认均为原行为;按业界 whipsaw 应对机制设计,治 5 条根因):
      buy_cool_down_days: 两次**买入**间最少交易日间隔(减仓不限)。
      max_target_step: 单次增仓目标最大上调(0-1),默认1.0;实测 0.2 帮倒忙(压仓踏空)。
      risk_confirm_days: risk 否决需连续 N 天才生效(机制1确认棒,治根因①);默认1=原行为。
      target_mode: "discrete"=阶跃查表(原);"continuous"=total 连续映射+双向滞回死区
        (机制7连续映射+机制2滞回,治根因②③=换手主因)。max_weight/total_dead 为其参数。
        (注:G10 后 action.compute_target_weight 已无 discrete 分支、仓位统一连续映射,
        target_mode 现仅记录到 metrics 归档、不参与仓位计算;参数保留待阶段3 执行层抽象清理。)
      min_trade_weight: 调仓幅度<此值(占总资产)不下单(机制7死区,治根因④);默认0。
      sell_cooldown_days: 部分减仓冷却天数(清仓/风险否决不限,机制4,治根因④);默认0。
      conf_gate: 弱 confidence(<此值)的清仓信号降级为维持(机制1 confidence gate,治根因⑤);默认0=关。
      debounce: StrategyDebounce(CompiledStrategy.debounce_params);传入时优先于各裸 kwargs,
                类型安全取代字符串 dict 耦合。scheduler 传它,旧调用方仍可用裸 kwargs(双入口)。
    """
    if valuation_basis not in ("raw", "qfq", "hfq"):
        raise ValueError(f"valuation_basis 必须是 raw/qfq/hfq,Got {valuation_basis!r}")
    credit_dividends = valuation_basis == "raw"   # raw 需显式补分红;qfq/hfq 已含分红,再入账=重复计息
    if debounce is not None:   # dataclass 覆盖各裸 kwargs(双入口向后兼容)
        buy_cool_down_days = debounce.buy_cool_down_days
        max_target_step = debounce.max_target_step
        risk_confirm_days = debounce.risk_confirm_days
        target_mode = debounce.target_mode
        max_weight = debounce.max_weight
        total_dead = debounce.total_dead
        min_trade_weight = debounce.min_trade_weight
        sell_cooldown_days = debounce.sell_cooldown_days
        conf_gate = debounce.conf_gate
        # 资金分配/风控:yaml risk 段可选配置(StrategyDebounce 字段 None=未配,用 engine 默认)
        _v = getattr(debounce, "max_gross", None)
        if _v is not None: max_gross = _v
        _v = getattr(debounce, "stop_loss_pct", None)
        if _v is not None: stop_loss_pct = _v
        _v = getattr(debounce, "portfolio_brake_dd", None)
        if _v is not None: portfolio_brake_dd = _v
        _v = getattr(debounce, "portfolio_brake_scale", None)
        if _v is not None: portfolio_brake_scale = _v
        _v = getattr(debounce, "portfolio_brake_mode", None)
        if _v is not None: portfolio_brake_mode = _v
        _v = getattr(debounce, "portfolio_brake_max_gross", None)
        if _v is not None: portfolio_brake_max_gross = _v
        _v = getattr(debounce, "portfolio_brake_keep_ratio", None)
        if _v is not None: portfolio_brake_keep_ratio = _v
        _v = getattr(debounce, "portfolio_brake_add_min_score", None)
        if _v is not None: portfolio_brake_add_min_score = _v
        _v = getattr(debounce, "portfolio_brake_recover_dd", None)
        if _v is not None: portfolio_brake_recover_dd = _v
        _v = getattr(debounce, "portfolio_brake_recover_high_days", None)
        if _v is not None: portfolio_brake_recover_high_days = _v
        _v = getattr(debounce, "portfolio_brake_tiers", None)
        if _v is not None: portfolio_brake_tiers = _v
        _v = getattr(debounce, "take_profit_tiers", None)
        if _v is not None: take_profit_tiers = _v
        _v = getattr(debounce, "take_profit_hard_pct", None)
        if _v is not None: take_profit_hard_pct = _v
        _v = getattr(debounce, "take_profit_atr_period", None)
        if _v is not None: take_profit_atr_period = _v
        _v = getattr(debounce, "take_profit_atr_tiers", None)
        if _v is not None: take_profit_atr_tiers = _v
        _v = getattr(debounce, "take_profit_atr_lagged", None)
        if _v is not None: take_profit_atr_lagged = _v
        _v = getattr(debounce, "market_regime_code", None)
        if _v is not None: market_regime_code = _v
        _v = getattr(debounce, "market_regime_ma_days", None)
        if _v is not None: market_regime_ma_days = _v
        _v = getattr(debounce, "market_regime_enter_band", None)
        if _v is not None: market_regime_enter_band = _v
        _v = getattr(debounce, "market_regime_exit_band", None)
        if _v is not None: market_regime_exit_band = _v
        _v = getattr(debounce, "market_regime_max_gross", None)
        if _v is not None: market_regime_max_gross = _v
        _v = getattr(debounce, "market_regime_target_vol", None)
        if _v is not None: market_regime_target_vol = _v
        _v = getattr(debounce, "market_regime_vol_window", None)
        if _v is not None: market_regime_vol_window = _v
        _v = getattr(debounce, "market_regime_vol_floor", None)
        if _v is not None: market_regime_vol_floor = _v
    portfolio_brake_scale = min(max(float(portfolio_brake_scale), 0.0), 1.5)
    if portfolio_brake_max_gross is not None:
        portfolio_brake_max_gross = min(
            max(float(portfolio_brake_max_gross), 0.0), 1.0,
        )
    if portfolio_brake_keep_ratio is not None:
        portfolio_brake_keep_ratio = min(
            max(float(portfolio_brake_keep_ratio), 0.0), 1.0,
        )
    if portfolio_brake_recover_dd is not None:
        portfolio_brake_recover_dd = min(
            max(float(portfolio_brake_recover_dd), 0.0), 1.0,
        )
    portfolio_brake_recover_high_days = max(
        int(portfolio_brake_recover_high_days or 0), 0,
    )
    # 深度分级刹车:((回撤阈值, 该档敞口上限), ...)按回撤深度升序;最深档兜底。
    if portfolio_brake_tiers:
        _norm = []
        for t in portfolio_brake_tiers:
            dd, cap = (float(t[0]), float(t[1])) if not isinstance(t, dict) else (
                float(t["drawdown"]), float(t["max_gross"]),
            )
            _norm.append((min(max(dd, 0.0), 1.0), min(max(cap, 0.0), 1.0)))
        portfolio_brake_tiers = tuple(sorted(_norm, key=lambda x: x[0]))
    if portfolio_brake_mode not in ("scale_all", "block_new_buys"):
        raise ValueError(
            "portfolio_brake_mode 必须是 scale_all 或 block_new_buys"
        )
    # 大盘趋势 regime 门禁参数规范化(前瞻性风控,与组合回撤刹车正交):
    # trend(ma_days)/ vol(target_vol)任一配置即启用;code 默认沪深300(回测基准)。
    if market_regime_ma_days is not None:
        market_regime_ma_days = max(int(market_regime_ma_days), 0)
    market_regime_enter_band = max(float(market_regime_enter_band), 0.0)
    market_regime_exit_band = max(float(market_regime_exit_band), 0.0)
    market_regime_max_gross = min(max(float(market_regime_max_gross), 0.0), 1.0)
    if market_regime_target_vol is not None:
        market_regime_target_vol = max(float(market_regime_target_vol), 0.0)
    market_regime_vol_window = max(int(market_regime_vol_window), 2)
    market_regime_vol_floor = min(max(float(market_regime_vol_floor), 0.0), 1.0)
    _regime_enabled = (
        (market_regime_ma_days is not None and market_regime_ma_days > 0)
        or (market_regime_target_vol is not None and market_regime_target_vol > 0)
    )
    if _regime_enabled and (market_regime_code is None or not market_regime_code):
        market_regime_code = BENCHMARK
    # 仓位调整层:独立基础架构,从 app_config 取(解耦于策略)
    from stockfu.ai.rebalancers import get_active_rebalancer, get_rebalancer_params
    rebalancer = get_active_rebalancer()
    rebalancer_params = get_rebalancer_params()
    # max_gross 优先级:yaml risk 显式配置(debounce.max_gross)> app_config rebalancer_params
    # > 默认。让 cap_and_rank 内部竞争额度与 engine 层安全阀用同一值,避免 pass_through/
    # top_n_picker 不限仓导致现金被吃光。YAML 显式配置(如 8 成仓)时优先,否则沿用 app_config。
    _yaml_max_gross = getattr(debounce, "max_gross", None) if debounce is not None else None
    _mp = rebalancer_params.get("max_gross")
    if _yaml_max_gross is not None:
        max_gross = float(_yaml_max_gross)
        rebalancer_params = {**rebalancer_params, "max_gross": max_gross}
    elif _mp is not None:
        max_gross = float(_mp)
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from stockfu.ai.action import PositionManager, resolve_action, compute_target_weight
    from stockfu.ai.analyze import analyze as default_analyze

    days = _trade_calendar_days(start, end)
    _analyze = analyze_fn or default_analyze
    acct = VirtualAccount(initial_cash)
    pm = PositionManager(buy_cool_down_days=buy_cool_down_days,
                         max_target_step=max_target_step,
                         min_trade_weight=min_trade_weight,
                         sell_cooldown_days=sell_cooldown_days)
    _risk_streak: dict[str, int] = {}  # code → risk 连续否决天数(确认棒状态)

    # 宇宙 + 可成交:研究模式默认严格交易约束(涨跌停/ST/list_date),与旧 strict=True 对齐。
    from stockfu.services.universe import DayFlags, UniverseContext, UniverseRules
    from stockfu.services.tradeability import ExecutionRules, check_fill, infer_pre_close
    if universe_rules is None:
        universe_rules = UniverseRules()
    if execution_rules is None:
        execution_rules = ExecutionRules()
    uni_ctx = UniverseContext.load(list(codes), universe_rules)
    universe_sizes: list[int] = []
    limit_reject_buys = 0
    limit_reject_sells = 0
    fill_rejects = 0
    deferred_orders = 0

    equity_curve: list[dict] = []
    holdings_curve: list[dict] = []          # 每日逐票持仓快照(完整持仓记录,供直观回看)
    trades: list[dict] = []
    pending_target: dict[str, float] = {}  # {code: target_weight} 待次日开盘执行
    pending_signal: dict[str, str | None] = {}  # 同生命周期:挂单的 signal(止损等),穿透到成交单
    last_close: dict[str, float] = {}       # code → 最近有收盘价交易日的价(停牌日估值用)
    peak_equity: float = float(initial_cash)  # 组合回撤刹车:追踪回测内权益峰值
    brake_latched: bool = False   # 刹车滞回:触发后需满足解除条件(防频繁开关)
    brake_eq_window: deque[float] = deque(  # 近期权益序列(解除参考 = 滚动 N 日新高)
        maxlen=max(int(portfolio_brake_recover_high_days), 1) or 1
    )
    cash_constraint_hits: int = 0             # 当日买单触发现金缩放的天数(可观测,对标 backtrader Margin)

    _atr_period = int(take_profit_atr_period or 0)
    _atr_enabled = _atr_period > 0 and bool(take_profit_atr_tiers)
    _atr_ranges: dict[str, deque[float]] = {}
    _atr_previous_close: dict[str, float] = {}


    # D: 区间行情列式预载(一次 SQL → _SeriesCtx:全局日历 + per-code array);日循环零扫库。
    # 预载起点提前 _PRELOAD_LOOKBACK_DAYS,覆盖算子最大回看(low_volatility ~1160 历日),
    # 使 _backtest_series_ctx 能从内存零查库喂给 quote_series(与 DB 逐值一致)。
    _pre_start = start - timedelta(days=_PRELOAD_LOOKBACK_DAYS)
    sctx = _preload_market_range(list(codes), _pre_start, end) if days else None
    # 用 start 之前已预载的行情预热 ATR,避免回测起点恰好落在波动期时产生
    # 人为的 20 日冷启动差异;只读取 <start 的历史,仍无未来函数。
    if _atr_enabled and sctx:
        for code, cols in sctx.series.items():
            history = deque(maxlen=_atr_period)
            previous = None
            valid_code = sctx.valid.get(code)
            for di, hist_date in enumerate(sctx.dates):
                if hist_date >= start:
                    break
                if valid_code is None or not valid_code[di]:
                    continue
                previous, _ = _update_atr_percent(
                    _bar_from_cols(cols, di), previous, history, _atr_period,
                )
            if history:
                _atr_ranges[code] = history
            if previous and previous > 0:
                _atr_previous_close[code] = previous
    # 分红预载:研究模式(non-strict 主线)只读 dividend_event。qfq/hfq 三复权价已含
    # 分红再投+送转(002594 实证:除权日 qfq/hfq 不跌),无需手动入账/调仓,再补=重复计息;
    # 仅 raw 口径(不复权)需把现金分红补进账户、把送转显式调股数。
    dividend_index = (_preload_dividend_events(list(codes), _pre_start, end)
                      if sctx else {})
    cash_dividends = (
        _preload_cash_dividends(list(codes), start, end)
        if sctx and credit_dividends else {}
    )
    stock_dividends = (_preload_stock_dividends(list(codes), start, end)
                       if sctx and credit_dividends else {})
    # 大盘趋势 regime 门禁:预载基准收盘序列(trend 算 MA / vol 算已实现波动率),
    # 一次 SQL;日循环 bisect 取窗,零查库(_pre_start 已含 1900 历日回看,覆盖 200 日均线)。
    _regime_bench = (_preload_bench_closes(market_regime_code, _pre_start, end)
                     if (_regime_enabled and sctx) else {"dates": [], "closes": []})
    _regime_bear_latched = False
    _regime_bear_days = 0
    _regime_throttle_days = 0
    if valuation_basis == "hfq" and sctx:
        cov, hit, tot = _hfq_coverage(sctx, start, end)
        if tot and cov < HFQ_COVERAGE_MIN:
            raise RuntimeError(
                f"hfq 口径门禁未过:close_hfq 覆盖率 {cov*100:.2f}% ({hit}/{tot}) < "
                f"{HFQ_COVERAGE_MIN*100:.1f}%。请先 backfill 三复权(close_hfq)或改用 "
                f"--valuation-basis raw 兜底(注意 raw 不含送转调整)。"
            )

    # 整段回测复用一个线程池(旧:每天 with 创建/销毁;冷 miss 并行在 prefetch 内,
    # analyze 热路径有 prefill 时串行,池仅兜底无 prefill 路径)。
    # _backtest_series_ctx 挂内存行情供给器 → 算子 quote_series 零查库(冷启提速核心)。
    # 1% 粒度进度日志(BACKTEST_PROGRESS=1 启用):每完成 1% 天数打印一行,含本 1% 耗时;flush 实时落日志。
    import os, time
    _prog_on = bool(os.environ.get("BACKTEST_PROGRESS"))
    _prog_total = len(days)
    _prog_step = max(1, _prog_total // 100) if _prog_total else 1
    _prog_i = 0
    _prog_last = -1
    _prog_t0 = time.time()
    with _backtest_series_ctx(sctx, dividend_index), ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
      for as_of in days:
        _prog_i += 1
        if _prog_on and _prog_i % _prog_step == 0:
            _pct = _prog_i * 100 // _prog_total
            if _pct != _prog_last:
                _now = time.time()
                print(f"  进度 {_pct}% ({_prog_i}/{_prog_total}) 本1%耗时 {round(_now - _prog_t0, 1)}s", flush=True)
                _prog_t0 = _now
                _prog_last = _pct
        close_prices, open_prices_day, day_bars = _get_day_market(
            list(codes), as_of, sctx=sctx, valuation_basis=valuation_basis)
        if not close_prices:
            continue
        atr_pct_by_code: dict[str, float] = {}
        if _atr_enabled:
            for code, bar in day_bars.items():
                history = _atr_ranges.setdefault(code, deque(maxlen=_atr_period))
                prior_atr_pct = (
                    sum(history) / len(history)
                    if len(history) >= _atr_period else None
                )
                previous, atr_pct = _update_atr_percent(
                    bar, _atr_previous_close.get(code), history, _atr_period,
                )
                if previous and previous > 0:
                    _atr_previous_close[code] = previous
                selected_atr_pct = prior_atr_pct if take_profit_atr_lagged else atr_pct
                if selected_atr_pct is not None:
                    atr_pct_by_code[code] = selected_atr_pct
        # 公司行为结算(研究模式 non-strict 主线):仅 raw 口径门控(详见 settle_dividends)。
        trades.extend(settle_dividends(acct, as_of, cash_dividends,
                                       stock_dividends, credit_dividends))
        last_close.update(close_prices)   # 停牌日 close 缺失时,沿用上一交易日价估值(不记 0)
        # 持仓峰值只在持仓期间更新；新开仓/清仓重置由 VirtualAccount.apply_action 处理。
        for code, pos in acct.positions.items():
            if pos.shares > 0 and close_prices.get(code, 0.0) > pos.peak_close:
                pos.peak_close = close_prices[code]

        # ---- Phase 1: 执行前日挂单(T+1 开盘价;停牌/涨跌停顺延或拒绝)----
        # 先卖后买 + 买单等比缩放到可用现金(对标 rqalpha order_target_portfolio_smart):
        #   卖单先成交释放现金 → 买单再用释放后的现金;买单总额 > 现金时用 safety 标量等比
        #   缩放,不逐笔 min(delta,cash) 夹断丢目标。各方向内部按 code 排序保跨进程可复现。
        if pending_target:
            open_prices = dict(open_prices_day)  # 当日 open 已与 close/bars 同次查出
            still_pending: dict[str, float] = {}
            still_signal: dict[str, str | None] = {}          # 顺延挂单的 signal 一起带过夜
            sells: list[tuple[str, float, float, str]] = []   # (code, target_weight, px, source)
            buys: list[tuple[str, float, float, str]] = []
            for code, target_weight in sorted(pending_target.items()):
                sig = pending_signal.get(code)               # 取该挂单的 signal(止损等),穿透到成交
                if code not in open_prices and code in close_prices:
                    open_prices[code] = close_prices[code]
                px, source = _get_trade_price(code, open_prices, close_prices)
                if px <= 0:
                    still_pending[code] = target_weight       # 停牌顺延,不丢信号
                    still_signal[code] = sig
                    deferred_orders += 1
                    continue
                act = resolve_action(acct.weight(code, open_prices), target_weight)
                if act == "hold":
                    continue
                side = "sell" if act in ("sell", "reduce") else "buy"
                bar = day_bars.get(code, {})
                fill = check_fill(
                    side, px,
                    pct_chg=bar.get("pct_chg"),
                    open_=bar.get("open_raw") or bar.get("open"),
                    high=bar.get("high_raw") or bar.get("high"),
                    low=bar.get("low_raw") or bar.get("low"),
                    close=bar.get("close_raw") or bar.get("close"),
                    board=uni_ctx.board(code),
                    is_st=bool(bar.get("is_st")),
                    trade_status=int(bar.get("trade_status", 1)),
                    pre_close=infer_pre_close(bar.get("close_raw") or bar.get("close"), bar.get("pct_chg")),
                    rules=execution_rules,
                )
                if not fill.ok:
                    fill_rejects += 1
                    if fill.reason == "limit_up_no_buy":
                        limit_reject_buys += 1
                    elif fill.reason == "limit_down_no_sell":
                        limit_reject_sells += 1
                    trades.append({
                        "date": as_of.isoformat(), "code": code, "kind": act,
                        "status": fill.status, "reason": fill.reason,
                        "target_weight": target_weight, "price": px,
                        "price_source": source,
                    })
                    if fill.status == "deferred":
                        still_pending[code] = target_weight
                        still_signal[code] = sig
                        deferred_orders += 1
                    continue
                if act in ("sell", "reduce"):
                    sells.append((code, target_weight, fill.price, source))
                elif act in ("buy", "add"):
                    buys.append((code, target_weight, fill.price, source))

            def _exec(code, tw, px, source, **extra):
                # signal 从 kwarg 取出(不入 apply_action 的 **extra,避免 kwarg 撞);
                # 取自挂单穿透的 pending_signal,让止损等 signal 正确落到成交单(原硬写 None 会丢)。
                sig = extra.pop("signal", None)
                tr = acct.apply_action(code, resolve_action(acct.weight(code, open_prices), tw),
                                        tw, px, open_prices, as_of=as_of)
                if tr:
                    tr.update(date=as_of.isoformat(), signal=sig, reason="open_exec",
                              price_source=source, status="filled", **extra)
                    trades.append(tr)

            # 1a. 先执行所有卖单(按 code 序)——释放现金给买单
            for code, tw, px, source in sells:
                _exec(code, tw, px, source, signal=pending_signal.get(code))
            # 1b. 买单等比缩放到可用现金(卖单释放后),再执行(按 code 索引,禁止 zip 错位)
            scaled, safety, constrained = scale_buys_to_cash(
                acct, [(c, tw, px) for c, tw, px, _ in buys], open_prices,
                commission_rate=COMMISSION_RATE, transfer_fee_rate=TRANSFER_FEE_RATE,
                min_commission=MIN_COMMISSION)
            if constrained:
                cash_constraint_hits += 1
            scaled_by_code = {c: (stw, spx) for c, stw, spx in scaled}
            for code, _tw, px, source in buys:
                stw, spx = scaled_by_code.get(code, (_tw, px))
                _exec(code, stw, spx, source,
                      signal=pending_signal.get(code),
                      **({"cash_scaled": round(safety, 4)} if constrained else {}))
            pending_target = still_pending
            pending_signal = still_signal

        # ---- Phase 2: 宇宙过滤 + 收盘快照 + 分析 ----
        # 日 flags:有 close 的票 has_row;is_st/trade_status 来自 bar
        day_flags: dict[str, DayFlags] = {}
        for c in codes:
            bar = day_bars.get(c)
            if bar:
                day_flags[c] = DayFlags(
                    is_st=bool(bar.get("is_st")),
                    trade_status=int(bar.get("trade_status", 1)),
                    has_row=True,
                    amount=bar.get("amount"),
                )
            else:
                day_flags[c] = DayFlags(has_row=False)
        # 宇宙:研究模式按 UniverseRules 过滤(默认严格:涨跌停/ST/list_date)+ 当日有收盘价才进截面
        u = {c for c in uni_ctx.eligible_on(as_of, day_flags) if c in close_prices}
        universe_sizes.append(len(u))

        total0 = acct.equity(close_prices)
        cash_r = acct.cash / total0 if total0 > 0 else 0.0  # noqa: F841
        snap: dict[str, dict] = {}
        # 分析集合 = 入选池 E(t) + 已持仓的持仓管理池 M(t)。调出成分不再进
        # E(t)，因此不能建仓/加仓；但它若仍有仓位，必须继续运行退出、止损和
        # 减仓规则，不能因调出而无限期冻结持仓。
        analyze_codes = set(u)
        for code in set(close_prices) | {
            c for c, p in acct.positions.items() if p.shares > 0
        }:
            pos = acct.positions.get(code)
            snap[code] = {
                "holding": {"shares": pos.shares, "avg_cost": pos.avg_cost}
                           if pos and pos.shares > 0 else None,
                "weight": acct.weight(code, close_prices if code in close_prices
                                      else {**last_close, **close_prices}),
                "in_universe": code in u,
            }
            if code in u or (pos and pos.shares > 0 and code in close_prices):
                analyze_codes.add(code)

        results: dict[str, dict] = {}
        # 单日批量预读(+冷 miss 填充):只预读宇宙内 codes
        prefill = prefetch_fn(list(analyze_codes), as_of) if prefetch_fn else None
        # 有 prefill 时 analyze 几乎全 hit(纯聚合,实测 ~0.05ms/票),线程池提交开销
        # 大于并行收益 → 串行;无 prefill 的兜底路径(实盘/旧调用)才用池并行。
        to_run = [c for c in analyze_codes if c in snap]
        if prefill is not None or max_workers <= 1:
            for c in to_run:
                try:
                    if prefill is not None:
                        results[c] = _analyze(c, as_of, snap[c]["holding"], prefill)
                    else:
                        results[c] = _analyze(c, as_of, snap[c]["holding"])
                except Exception:  # noqa: BLE001
                    pass
        else:
            fut = {pool.submit(_analyze, c, as_of, snap[c]["holding"]): c for c in to_run}
            for f in as_completed(fut):
                c = fut[f]
                try:
                    results[c] = f.result()
                except Exception:  # noqa: BLE001
                    pass

        # ---- Phase 3: 仓位层(信号→desired→组合层→目标仓位→边沿触发→冷却) ----
        # 3a. 逐标的算 desired。宇宙外持仓走 exit-only：仍跑卖出/止损/减仓，
        # 但绝不可建仓或加仓；非持仓的宇宙外股票完全不参与信号。
        desired: dict[str, float | None] = {}
        meta: dict[str, dict] = {}
        _sig: dict[str, str] = {}      # 记 signal/risk_vetoed 供 3c trade 记录用
        _veto: dict[str, bool] = {}
        for code in snap:
            current_w = snap[code]["weight"]
            in_u = code in u

            # 宇宙外且未持仓：不是候选，也不进入持仓管理池。
            if not in_u:
                fl = day_flags.get(code)
                if (universe_rules.exclude_st and fl and fl.is_st and current_w > 0):
                    desired[code] = 0.0
                    _sig[code] = "universe_st_exit"
                    _veto[code] = False
                    meta[code] = {"score": None, "confidence": None,
                                  "signal": "universe_st_exit", "risk_vetoed": False,
                                  "raw": None, "exit_only": True}
                    continue
                if current_w <= 0:
                    continue

            r = results.get(code)
            if not r or "error" in r or not r.get("aggregate"):
                continue
            agg = r["aggregate"]
            signal = agg.get("final_signal", "hold")
            risk_vetoed = agg.get("risk_vetoed", False)
            ai_target = agg.get("ai_target_weight")
            total_score = agg.get("total_score")
            confidence = agg.get("confidence")

            # risk 否决确认棒:连续 N 天才生效(N=1=原行为),过滤单日抖动(头号翻转源)
            if risk_confirm_days > 1:
                if risk_vetoed:
                    _risk_streak[code] = _risk_streak.get(code, 0) + 1
                else:
                    _risk_streak[code] = 0
                risk_vetoed = _risk_streak[code] >= risk_confirm_days

            # 信号→目标仓位(discrete=阶跃查表;continuous=total 连续映射+滞回死区)
            # 买卖权重不对称(opt-in):runner 已归一化买入分 total_score_norm 与卖出分
            # total_sell_score(±100 刻度)时用双总分滞回;否则回退原始 total_score(旧路径)。
            total_score_norm = agg.get("total_score_norm")
            total_sell_score = agg.get("total_sell_score")
            target_weight = compute_target_weight(
                risk_vetoed, current_w, ai_target,
                total_score=total_score_norm if total_score_norm is not None else total_score,
                total_sell_score=total_sell_score,
                max_w=max_weight, dead=total_dead,
                score_full=debounce.score_full if debounce else 20.0,
            )

            # confidence gate:弱 confidence 的清仓信号降级为维持(防 total 抖动误清仓);
            # 建仓/加仓不 gate(鼓励吃行情)。conf_gate=0 关闭。
            if (conf_gate > 0 and target_weight == 0.0 and current_w > 0
                    and confidence is not None and confidence < conf_gate):
                target_weight = None
                signal = "hold"

            # 个股成本止损(规则化风控,补 BACKTEST.md 承诺但缺失的代码):浮亏达 stop_loss → 强制清仓。
            # stop_loss_pct=0 关闭;仅对持仓且策略想持有/加仓(target>0)时介入,不与已清仓重复。
            if stop_loss_pct > 0 and current_w > 0 and target_weight not in (0.0, None):
                _pos = acct.positions.get(code)
                _px = close_prices.get(code, 0.0)
                if (_pos and _pos.shares > 0 and _pos.avg_cost > 0 and _px > 0
                        and _px / _pos.avg_cost - 1 <= -stop_loss_pct):
                    target_weight = 0.0
                    signal = "stop_loss"

            # 分级追踪止盈是可选风控；二元 tier 仍按旧语义全清，带 sell_fraction 的
            # tier 则按首次触发前持仓分段减仓，并把剩余仓位设为上限，防止次日买回。
            _pos = acct.positions.get(code)
            _px = close_prices.get(code, 0.0)
            _active_tp_tiers = (
                take_profit_atr_tiers if _atr_enabled else take_profit_tiers
            )
            _has_partial_tp = any(
                (parts := (_take_profit_atr_parts(tier)
                           if _atr_enabled else _take_profit_tier_parts(tier))) is not None
                and parts[2] < 1.0
                for tier in _active_tp_tiers
            )
            if (_pos and current_w > 0
                    and (target_weight not in (0.0, None)
                         or (target_weight is None and _has_partial_tp))):
                if _atr_enabled:
                    _tp_action = atr_take_profit_action(
                        _pos.avg_cost, _pos.peak_close, _px,
                        atr_pct_by_code.get(code), take_profit_atr_tiers,
                        take_profit_hard_pct, _pos.take_profit_fired,
                    )
                else:
                    _tp_action = tiered_take_profit_action(
                        _pos.avg_cost, _pos.peak_close, _px,
                        take_profit_tiers, take_profit_hard_pct,
                        _pos.take_profit_fired,
                    )
                if _tp_action:
                    _tp_reason, _sell_fraction, _tp_stage = _tp_action
                    signal = _tp_reason
                    if _sell_fraction >= 1.0 - 1e-12:
                        target_weight = 0.0
                    else:
                        if _pos.take_profit_anchor_shares <= 0:
                            _pos.take_profit_anchor_shares = _pos.shares
                        if _tp_stage:
                            _pos.take_profit_fired.add(_tp_stage)
                        _remaining = _take_profit_remaining_fraction(
                            _active_tp_tiers, _pos.take_profit_fired,
                            atr=_atr_enabled,
                        )
                        _cap_shares = int(
                            _pos.take_profit_anchor_shares * _remaining / 100
                        ) * 100
                        if _remaining > 0 and _cap_shares <= 0:
                            _cap_shares = min(100, _pos.shares)
                        if _pos.take_profit_cap_shares is None:
                            _pos.take_profit_cap_shares = _cap_shares
                        else:
                            _pos.take_profit_cap_shares = min(
                                _pos.take_profit_cap_shares, _cap_shares,
                            )
                        _eq = acct.equity(last_close)
                        _cap_value = (
                            _pos.take_profit_cap_shares + _pos.receivable_shares
                        ) * _px
                        _cap_weight = _cap_value / _eq if _eq > 0 else 0.0
                        target_weight = (
                            _cap_weight if target_weight is None
                            else min(target_weight, _cap_weight)
                        )

            # 已经分段减仓的持仓保持在该阶段的仓位上限内；正常因子目标可以
            # 进一步减仓，但不能把已兑现的仓位重新补回去。
            if (_pos and _pos.take_profit_cap_shares is not None
                    and target_weight not in (0.0, None) and _px > 0):
                _eq = acct.equity(last_close)
                _cap_value = (
                    _pos.take_profit_cap_shares + _pos.receivable_shares
                ) * _px
                _cap_weight = _cap_value / _eq if _eq > 0 else 0.0
                target_weight = min(target_weight, _cap_weight)

            # 调出后的持仓：让策略的正常卖出、止损、减仓生效，但任何正向目标都
            # 不得超过现有仓位。exit_only 也传给 TopN，确保它不占用新选名额、不会
            # 被组合层按排名当作候选股重新加仓。
            exit_only = not in_u
            if exit_only and target_weight is not None:
                target_weight = min(target_weight, current_w)

            desired[code] = target_weight
            _sig[code] = signal
            _veto[code] = risk_vetoed
            meta[code] = {"score": total_score, "confidence": confidence,
                          "signal": signal, "risk_vetoed": risk_vetoed,
                          "raw": total_score, "exit_only": exit_only}

        # 3b. 仓位调整层:desired全集 + current全集 → 最终目标仓位(独立基础架构,从 app_config 取)
        current_weights = {c: s["weight"] for c, s in snap.items()}   # 全集(含未覆盖持仓)
        # 增强刹车参数是否显式配置(opt-in):配置任一即走组合级敞口刹车新路径;
        # 全部未配时保持旧语义(仅缩正目标/禁新买,rebalancer 每轮仍填满 max_gross)。
        _enhanced_brake = (
            portfolio_brake_max_gross is not None
            or bool(portfolio_brake_tiers)
            or portfolio_brake_keep_ratio is not None
            or portfolio_brake_add_min_score is not None
            or portfolio_brake_recover_dd is not None
            or portfolio_brake_recover_high_days > 0
            or _regime_enabled
        )
        _cur_eq = acct.equity(last_close)
        peak_equity = max(peak_equity, _cur_eq)
        _below_brake = (
            portfolio_brake_dd > 0
            and peak_equity > 0
            and _cur_eq / peak_equity - 1 <= -portfolio_brake_dd
        )
        # 滞回解除(仅增强路径,防频繁开关):
        #   - recover_high_days>0:权益创出滚动 N 日新高即解除(临时熔断,自释放);
        #   - 否则 recover_dd 设置时需恢复到峰值 -recover_dd 以内;
        #   - 两者都未配时回到触发线上方即解除(旧语义)。
        _rolling_high = max(brake_eq_window) if brake_eq_window else 0.0
        if _below_brake:
            brake_latched = True
        elif portfolio_brake_recover_high_days > 0:
            if brake_latched and _cur_eq >= _rolling_high:
                brake_latched = False
        elif portfolio_brake_recover_dd is None:
            brake_latched = False
        elif brake_latched and _cur_eq / peak_equity - 1 > -portfolio_brake_recover_dd:
            brake_latched = False
        _brake_active = bool(_below_brake or brake_latched)
        # 记录当日权益进滚动窗口(滚到解除判定之后,避免当日自身成新高)。
        brake_eq_window.append(_cur_eq)

        if not _enhanced_brake:
            # 旧路径(逐字节保持):rebalancer 用原 max_gross;刹车只缩正目标/禁新买。
            final = rebalancer.adjust(
                desired, current_weights, meta,
                equity=acct.equity(last_close),
                params=rebalancer_params,
            )
            # 宇宙外持仓:禁止加仓(target 上限 = current);允许 exit-only 信号减仓/清仓。
            for code, tw in list(final.items()):
                if code in u or tw is None:
                    continue
                cur = current_weights.get(code, 0.0)
                if tw > cur + 1e-9:
                    final[code] = cur
            if _brake_active:
                if portfolio_brake_mode == "block_new_buys":
                    # 选择性刹车:新增仓/已有仓加仓被压回当前仓位;正常减仓、止损、止盈放行。
                    final = _block_portfolio_new_buys(final, current_weights)
                else:
                    final = {c: (w * portfolio_brake_scale if w else w)
                             for c, w in final.items()}
            final = _apply_gross_cap(final, max_gross)
        else:
            # 组合级敞口刹车:刹车期把 rebalancer 竞争额度收紧到有效 max_gross,
            # 否则 cap_and_rank 先按 max_gross 填满、之后单票缩放被每日重新填满
            # → 总敞口实际不降(2008 实测根因)。
            _effective_max_gross = max_gross
            _day_rebalancer_params = rebalancer_params
            if _brake_active:
                if portfolio_brake_tiers:
                    # 深度分级:按当前回撤深度取对应敞口上限(越深越紧,自然随反弹放松)。
                    _dd = _cur_eq / peak_equity - 1.0
                    _effective_max_gross = max_gross
                    for _t_dd, _t_cap in portfolio_brake_tiers:
                        if _dd <= -_t_dd:
                            _effective_max_gross = min(_t_cap, max_gross)
                        else:
                            break
                elif portfolio_brake_max_gross is not None:
                    # 显式配的组合级敞口:平滑刹车(<max_gross)或回撤加仓(>max_gross)共用。
                    _effective_max_gross = portfolio_brake_max_gross
                elif portfolio_brake_mode == "block_new_buys":
                    _effective_max_gross = max_gross
                else:
                    _effective_max_gross = min(
                        max_gross * portfolio_brake_scale, max_gross,
                    )
                _day_rebalancer_params = dict(rebalancer_params)
                _day_rebalancer_params["max_gross"] = _effective_max_gross
            # 大盘趋势 regime 门禁:前瞻性敞口 cap,min 叠加在组合回撤刹车之上
            # (brake 不 active 时也独立生效;温市满仓、危机初期降仓)。
            if _regime_enabled:
                _win = _bench_closes_asof(
                    _regime_bench, as_of,
                    max(market_regime_ma_days or 0, market_regime_vol_window + 1, 252),
                )
                _cap, _regime_bear_latched = _market_throttle_step(
                    _win, bear_latched=_regime_bear_latched,
                    ma_days=market_regime_ma_days,
                    enter_band=market_regime_enter_band,
                    exit_band=market_regime_exit_band,
                    bear_gross=market_regime_max_gross,
                    target_vol=market_regime_target_vol,
                    vol_window=market_regime_vol_window,
                    vol_floor=market_regime_vol_floor,
                    max_gross=max_gross,
                )
                if _cap < _effective_max_gross - 1e-12:
                    _effective_max_gross = _cap
                    _day_rebalancer_params = dict(_day_rebalancer_params)
                    _day_rebalancer_params["max_gross"] = _effective_max_gross
                if market_regime_ma_days and _regime_bear_latched:
                    _regime_bear_days += 1
                if _cap < max_gross - 1e-9:
                    _regime_throttle_days += 1
            final = rebalancer.adjust(
                desired, current_weights, meta,
                equity=acct.equity(last_close),
                params=_day_rebalancer_params,
            )
            # 宇宙外持仓:禁止加仓(target 上限 = current);允许 exit-only 信号减仓/清仓。
            for code, tw in list(final.items()):
                if code in u or tw is None:
                    continue
                cur = current_weights.get(code, 0.0)
                if tw > cur + 1e-9:
                    final[code] = cur
            # 刹车期保留配置比例目标仓位:组合级敞口收窄 / 质量门控 / 回撤加仓门控。
            if _brake_active:
                final = _apply_portfolio_brake(
                    final, current_weights, meta,
                    scale=portfolio_brake_scale,
                    mode=portfolio_brake_mode,
                    brake_max_gross=_effective_max_gross,
                    keep_ratio=portfolio_brake_keep_ratio,
                    add_min_score=portfolio_brake_add_min_score,
                    max_weight=max_weight,
                )
            # 总仓安全阀:Σ目标权重 ≤ max_gross(留 1-max_gross 现金,对所有 rebalancer 生效)→
            # 保证执行层买单总额 ≤ 可投资现金,不夹断丢目标。超限等比缩放所有正值权重。
            final = _apply_gross_cap(final, _effective_max_gross)

        # 3c. 边沿触发 + 冷却(遍历 final 全集;未覆盖维持的 code 过 should_act 是 no-op)
        # sorted by code:final 经 rebalancer 的 set 构造、顺序随哈希随机化漂移;
        # 排序后挂单入 pending_target 的序确定 → 次日 Phase 1 执行序确定(见上)。
        for code, target_weight in sorted(final.items()):
            current_w = current_weights[code]
            should, target, reason = pm.should_act(
                code, target_weight, current_w, as_of, days,
            )
            if should:
                pending_target[code] = target
                pending_signal[code] = _sig.get(code)
                trades.append({
                    "date": as_of.isoformat(),
                    "code": code,
                    "signal": _sig.get(code),
                    "risk_vetoed": _veto.get(code),
                    "target_weight": round(target, 4) if target is not None else None,
                    "reason": reason,
                    "status": "pending",
                })

        # ---- Record: 收盘净值(停牌持仓用 last_close 估值,不记 0) ----
        equity_curve.append({
            "date": as_of.isoformat(),
            "equity": round(acct.equity(last_close), 2),
        })
        # ---- Record: 逐票持仓快照(完整持仓记录;停牌持仓用 last_close 估值,不漏) ----
        eq_total = acct.equity(last_close)
        day_pos = []
        for c, p in acct.positions.items():
            if p.shares <= 0 and p.receivable_shares <= 0:
                continue
            px = close_prices.get(c) or last_close.get(c, 0.0)
            mv = (p.shares + p.receivable_shares) * px
            day_pos.append({
                "code": c,
                "shares": p.shares,
                "receivable_shares": p.receivable_shares,
                "avg_cost": round(p.avg_cost, 4),
                "close": round(px, 4),
                "mkt_val": round(mv, 2),
                "pnl": round(mv - p.shares * p.avg_cost, 2),   # 浮动盈亏(未扣费)
                "weight": round(mv / eq_total, 4) if eq_total > 0 else 0.0,
            })
        day_pos.sort(key=lambda x: -x["mkt_val"])
        holdings_curve.append({
            "date": as_of.isoformat(),
            "cash": round(acct.cash, 2),
            "cash_receivable": round(acct.cash_receivable, 2),
            "equity": round(eq_total, 2),
            "positions": day_pos,
        })
      # for as_of 结束;with 退出时 pool.shutdown

    # ---- 绩效 ----
    benchmark, bench_window = _benchmark_curve(BENCHMARK, days)
    # 成交笔=有 shares 的执行记录(排除 pending 意图 / rejected / deferred)
    filled = [t for t in trades if isinstance(t.get("shares"), (int, float))]
    win = [t for t in filled if t.get("pnl") is not None and t["pnl"] > 0]
    loss = [t for t in filled if t.get("pnl") is not None and t["pnl"] <= 0]

    metrics = _metrics(equity_curve, benchmark, initial_cash, len(days),
                        bench_window=bench_window)
    metrics["trade_count"] = len(filled)
    metrics["win_rate"] = round(len(win) / (len(win) + len(loss)) * 100, 1) if (win or loss) else None
    # 成交类对比指标(signal 直传精确值,见下方 _exec 信号穿透):
    # distinct_stocks_bought=去重后曾买入的不同股票数;
    # stop_loss=signal=="stop_loss" 的已成交单(止损 D+1~D+3 成交),realized_loss=其 pnl 之和。
    metrics["distinct_stocks_bought"] = len({
        t["code"] for t in filled if t.get("kind") in ("buy", "add")
    })
    _sl_filled = [t for t in filled if t.get("signal") == "stop_loss"]
    metrics["stop_loss_count"] = len(_sl_filled)
    metrics["stop_loss_realized_loss"] = round(
        sum((t.get("pnl") or 0.0) for t in _sl_filled), 2
    )  # 负数=亏损(元);pnl 符号:盈>0 亏<0(与上方 win/loss 判定一致)
    _tp_filled = [t for t in filled if str(t.get("signal", "")).startswith("take_profit_")]
    metrics["take_profit_count"] = len(_tp_filled)
    metrics["take_profit_realized_pnl"] = round(sum((t.get("pnl") or 0.0) for t in _tp_filled), 2)
    metrics["total_fee"] = round(acct.fee_paid, 2)
    metrics["cash_dividend_gross"] = round(acct.dividend_received, 2)
    metrics["dividend_tax_paid"] = round(acct.dividend_tax_paid, 2)
    metrics["cash_dividend_net"] = round(acct.dividend_received - acct.dividend_tax_paid, 2)
    metrics["cash_dividend_receivable"] = round(acct.cash_receivable, 2)
    # 组合层指标(从 holdings_curve 算,对标 zipline ledger gross leverage + 单仓集中度):
    _gross = [sum(p["weight"] for p in d.get("positions", [])) for d in holdings_curve]
    metrics["avg_gross_leverage"] = round(sum(_gross) / len(_gross) * 100, 1) if _gross else None
    metrics["max_gross_leverage"] = round(max(_gross) * 100, 1) if _gross else None
    metrics["max_single_weight"] = round(
        max((p["weight"] for d in holdings_curve for p in d.get("positions", [])), default=0.0) * 100, 1)
    # 换手:相邻交易日持仓 code 集合的对称差(单边换手 = 对称差/2:换1只=卖1买1=对称差2)。
    # 降换手策略核心观测量。turnover_count=回测期总换手只数(单边);
    # avg_daily_turnover=日均换手只数;annual_turnover=年化换手倍数(单边年换手/平均持仓数,
    # 1.0=组合一年换一遍)。原"五福"高频轮动年换手~20+遍,本策略目标降到个位数遍。
    _tov_total = 0.0
    _n_held: list[int] = []
    for _i in range(1, len(holdings_curve)):
        _prev = {p["code"] for p in holdings_curve[_i - 1].get("positions", [])}
        _cur = {p["code"] for p in holdings_curve[_i].get("positions", [])}
        _tov_total += len(_prev ^ _cur) / 2.0
        _n_held.append(len(_cur))
    _hold_days = max(len(holdings_curve) - 1, 1)
    metrics["turnover_count"] = round(_tov_total, 1)
    metrics["avg_daily_turnover"] = round(_tov_total / _hold_days, 2)
    _avg_n = sum(_n_held) / len(_n_held) if _n_held else 0.0
    _years = len(days) / 252.0
    metrics["annual_turnover"] = (round((_tov_total / _years) / _avg_n, 2)
                                  if _years > 0 and _avg_n > 0 else None)
    metrics["cash_constraint_hits"] = cash_constraint_hits   # 买单被现金缩放的天数(可观测)
    metrics["market_regime_bear_days"] = _regime_bear_days if _regime_enabled else 0
    metrics["market_regime_throttle_days"] = _regime_throttle_days if _regime_enabled else 0
    metrics["market_regime_bear_ratio"] = (
        round(_regime_bear_days / len(days), 4) if _regime_enabled and days else None
    )
    metrics["limit_reject_buys"] = limit_reject_buys
    metrics["limit_reject_sells"] = limit_reject_sells
    metrics["fill_rejects"] = fill_rejects
    metrics["deferred_orders"] = deferred_orders
    metrics["final_equity"] = round(
        acct.equity(last_close) if last_close else initial_cash, 2
    )
    metrics["config"] = {
        "buy_cool_down_days": buy_cool_down_days,
        "max_target_step": max_target_step,
        "risk_confirm_days": risk_confirm_days,
        "target_mode": target_mode,
        "max_weight": max_weight,
        "total_dead": total_dead,
        "min_trade_weight": min_trade_weight,
        "sell_cooldown_days": sell_cooldown_days,
        "conf_gate": conf_gate,
        "max_gross": max_gross,
        "stop_loss_pct": stop_loss_pct,
        "portfolio_brake_dd": portfolio_brake_dd,
        "portfolio_brake_scale": portfolio_brake_scale,
        "portfolio_brake_mode": portfolio_brake_mode,
        "portfolio_brake_max_gross": portfolio_brake_max_gross,
        "portfolio_brake_keep_ratio": portfolio_brake_keep_ratio,
        "portfolio_brake_add_min_score": portfolio_brake_add_min_score,
        "portfolio_brake_recover_dd": portfolio_brake_recover_dd,
        "portfolio_brake_recover_high_days": portfolio_brake_recover_high_days,
        "portfolio_brake_tiers": [list(t) for t in portfolio_brake_tiers],
        "market_regime_code": market_regime_code if _regime_enabled else None,
        "market_regime_ma_days": market_regime_ma_days,
        "market_regime_enter_band": market_regime_enter_band,
        "market_regime_exit_band": market_regime_exit_band,
        "market_regime_max_gross": market_regime_max_gross,
        "market_regime_target_vol": market_regime_target_vol,
        "market_regime_vol_window": market_regime_vol_window,
        "market_regime_vol_floor": market_regime_vol_floor,
        "take_profit_tiers": take_profit_tiers,
        "take_profit_hard_pct": take_profit_hard_pct,
        "take_profit_atr_period": take_profit_atr_period,
        "take_profit_atr_tiers": take_profit_atr_tiers,
        "take_profit_atr_lagged": take_profit_atr_lagged,
        "execution": "T+1_open_sell_first",
        "rebalancer": rebalancer.rebalancer_id,
        "universe": uni_ctx.summary(universe_sizes),
        "execution_rules": execution_rules.to_dict(),
        "valuation_basis": valuation_basis,
    }

    return {
        "equity_curve": equity_curve,
        "holdings_curve": holdings_curve,
        "benchmark": benchmark,
        "trades": trades,
        "metrics": metrics,
        "codes": list(codes),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "initial_cash": initial_cash,
        "days": len(days),
        "universe": uni_ctx.summary(universe_sizes),
        "valuation_basis": valuation_basis,
    }
