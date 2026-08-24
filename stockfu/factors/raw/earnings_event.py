"""earnings_event:财报事件因子(SUE 随机游走 / JOR 盈余跳空 / rec_acc 公告漂移)。

2026-08-18 接入,IC 快验 scripts/earnings_event_ic.py(月频截面,三段):

- ``sue_rw``   标准化未预期盈余(随机游走版):ΔTTM 净利 / 过去 8 个 ΔTTM 的
  std(去均值)。无一致预期数据,用 SUE-RW 代理(Ball-Brown PEAD 家族)。
  IC:2013-19 +0.037@20d(ICIR +2.6)强,2020+ 衰减至 0 → direction=
  higher_is_better 但作为组合分量权宜。
- ``jor``      盈余跳空:最近财报 pub_date 后首个交易日 open_raw/前收 - 1(%),
  事件窗外(>63 交易日)无值。东方证券口径。IC 两段一致为负
  (-0.013~-0.018@5d):公告日高开=利好兑现/散户抢筹,未来跑输 →
  direction=lower_is_better。
- ``rec_acc_rev``  公告后累积漂移的反向:首日至 t 收益取负。IC 两段强负
  (-0.031~-0.068@5d):公告后已涨的票继续跑输(A 股 PEAD 短、兑现快)。
  与 jor 同向(都吃"公告后不追高"),合成一个维度。

PIT 硬保证:只取 pub_date <= as_of 的财报行(available_at=pub_date);
JOR/rec_acc 的"公告后首日"在 as_of 当日或之前完成,引擎 T+1 开盘成交
天然不偷价。
"""
from __future__ import annotations

from datetime import date

from sqlmodel import select

from stockfu.db import session_scope
from stockfu.factors.raw import raw_fingerprint
from stockfu.models import FinancialProfit, QuoteSnapshot
from stockfu.scoring.contracts import MissingReason, RawFactorObservation

METRIC_SUE = "sue_rw"
METRIC_JOR = "jor"
METRIC_RECACC = "rec_acc_rev"

SUE_LOOKBACK = 8
JOR_HOLD_DAYS = 63

# 一个回测 worker 会在多个 as_of 上反复访问同一股票的财报。缓存原始公告行，
# 再在 Python 中按 as_of 做 PIT 截断；这样每个 worker 每只股票只查一次数据库，
# 不改变「pub_date <= as_of」的可用性约束。
_FINANCIAL_ROWS_CACHE: dict[
    str, tuple[tuple[date, int, int, float], ...]
] = {}
_QUOTE_ROWS_CACHE: dict[
    str, tuple[tuple[date, float | None, float | None, float | None], ...]
] = {}


def _insufficient(metric_id: str, code: str, as_of: date, fp: str,
                  diag: dict) -> RawFactorObservation:
    unit = {"sue_rw": "z_score", "jor": "percent", "rec_acc_rev": "percent"}.get(
        metric_id, "percent")
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=metric_id,
        raw_value=None, raw_unit=unit, source_max_date=as_of,
        available_at=as_of, valid=False,
        missing_reason=MissingReason.INSUFFICIENT_SAMPLES, raw_fingerprint=fp,
        diagnostics=diag)


def _financial_rows(code: str) -> tuple[tuple[date, int, int, float], ...]:
    cached = _FINANCIAL_ROWS_CACHE.get(code)
    if cached is not None:
        return cached
    with session_scope() as s:
        rows = s.exec(
            select(FinancialProfit).where(
                FinancialProfit.asset_code == code,
                FinancialProfit.pub_date != None,  # noqa: E711
                FinancialProfit.net_profit != None,  # noqa: E711
            ).order_by(FinancialProfit.pub_date, FinancialProfit.stat_date)
        ).all()
    packed = tuple(
        (r.pub_date, int(r.year), int(r.quarter), float(r.net_profit))
        for r in rows
        if r.pub_date is not None
        and r.year is not None
        and r.quarter is not None
        and r.net_profit is not None
    )
    _FINANCIAL_ROWS_CACHE[code] = packed
    return packed


def _ttm_series(code: str, as_of: date) -> list[tuple[date, float]]:
    """PIT 的 (pub_date, TTM 净利) 升序序列:最近四季净利之和。

    同一 (year, quarter) 多次披露(更正公告)保留最新 pub 的行;只取
    pub_date <= as_of,天然防未来。
    """
    by_period: dict[tuple[int, int], tuple[date, float]] = {}
    for pub, year, quarter, net_profit in _financial_rows(code):
        if pub > as_of:
            continue
        by_period[(year, quarter)] = (pub, net_profit)
    periods = sorted(by_period)
    out: list[tuple[date, float]] = []
    for i in range(3, len(periods)):
        window = periods[i - 3:i + 1]
        ttm = sum(by_period[p][1] for p in window)
        pub = max(by_period[p][0] for p in window)
        out.append((pub, ttm))
    return out


def _quote_rows(
    code: str, as_of: date,
) -> tuple[tuple[date, float | None, float | None, float | None], ...]:
    """缓存 raw open/close 与 qfq close；新日期在 live 场景按需追加。"""
    cached = _QUOTE_ROWS_CACHE.get(code)
    if cached is None:
        with session_scope() as s:
            rows = s.exec(
                select(
                    QuoteSnapshot.quote_date,
                    QuoteSnapshot.open_raw,
                    QuoteSnapshot.close_raw,
                    QuoteSnapshot.close_qfq,
                    QuoteSnapshot.close,
                ).where(
                    QuoteSnapshot.asset_code == code,
                ).order_by(QuoteSnapshot.quote_date)
            ).all()
        cached = tuple(
            (
                row[0],
                float(row[1]) if row[1] is not None else None,
                float(row[2]) if row[2] is not None else None,
                float(row[3] if row[3] is not None else row[4])
                if (row[3] is not None or row[4] is not None) else None,
            )
            for row in rows
        )
        _QUOTE_ROWS_CACHE[code] = cached
        return cached

    # 快照通常不可变；常驻进程进入新日期时，只追加新行，不重新扫描历史。
    if cached and as_of > cached[-1][0]:
        with session_scope() as s:
            rows = s.exec(
                select(
                    QuoteSnapshot.quote_date,
                    QuoteSnapshot.open_raw,
                    QuoteSnapshot.close_raw,
                    QuoteSnapshot.close_qfq,
                    QuoteSnapshot.close,
                ).where(
                    QuoteSnapshot.asset_code == code,
                    QuoteSnapshot.quote_date > cached[-1][0],
                    QuoteSnapshot.quote_date <= as_of,
                ).order_by(QuoteSnapshot.quote_date)
            ).all()
        appended = tuple(
            (
                row[0],
                float(row[1]) if row[1] is not None else None,
                float(row[2]) if row[2] is not None else None,
                float(row[3] if row[3] is not None else row[4])
                if (row[3] is not None or row[4] is not None) else None,
            )
            for row in rows
        )
        if appended:
            cached = cached + appended
            _QUOTE_ROWS_CACHE[code] = cached
    return cached


def compute_sue_rw(code: str, as_of: date, lookback: int = SUE_LOOKBACK,
                   basis: str = "net_profit_ttm",
                   metric_id: str = METRIC_SUE) -> RawFactorObservation:
    """SUE 随机游走:最新 ΔTTM 相对历史 ΔTTM 分布的 z 分数。"""
    lookback = int(lookback)
    if lookback < 4:
        raise ValueError("sue_rw 的 lookback 至少为 4")
    if basis != "net_profit_ttm":
        raise ValueError("当前 sue_rw 只支持 basis=net_profit_ttm")
    fp = raw_fingerprint(metric_id, "delta_ttm_zscore",
                         {"lookback": lookback, "basis": basis})
    ttms = _ttm_series(code, as_of)
    if len(ttms) < 5:
        return _insufficient(metric_id, code, as_of, fp, {"n_ttm": len(ttms)})
    idx = len(ttms) - 1
    deltas = []
    for j in range(max(1, idx - lookback + 1), idx + 1):
        deltas.append(ttms[j][1] - ttms[j - 1][1])
    if len(deltas) < 4:
        return _insufficient(metric_id, code, as_of, fp, {"n_deltas": len(deltas)})
    mu = sum(deltas) / len(deltas)
    var = sum((x - mu) ** 2 for x in deltas) / max(len(deltas) - 1, 1)
    sd = var ** 0.5
    if sd <= 1e-9:
        return _insufficient(metric_id, code, as_of, fp, {"sd": sd})
    latest = ttms[idx][1] - ttms[idx - 1][1]
    z = (latest - mu) / sd
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=metric_id,
        raw_value=float(z), raw_unit="z_score",
        source_max_date=ttms[idx][0], available_at=ttms[idx][0],
        valid=True, raw_fingerprint=fp, lookback_observations=len(deltas),
        diagnostics={"n_deltas": len(deltas), "pub": ttms[idx][0].isoformat()})


def _event_day(code: str, as_of: date) -> date | None:
    """最近一次"换 TTM 点"的公告日(相对上一 TTM 点的新披露)。"""
    ttms = _ttm_series(code, as_of)
    if len(ttms) < 2:
        return None
    return ttms[-1][0]


def compute_jor(code: str, as_of: date, hold_days: int = JOR_HOLD_DAYS,
                basis: str = "raw",
                metric_id: str = METRIC_JOR) -> RawFactorObservation:
    """JOR:公告后首个交易日跳空(%)。事件窗 63 交易日内有效,窗外缺失。"""
    hold_days = int(hold_days)
    if hold_days <= 0:
        raise ValueError("jor 的 hold_days 必须为正")
    if basis != "raw":
        raise ValueError("当前 jor 只支持 basis=raw")
    fp = raw_fingerprint(metric_id, "gap_after_earnings_pub",
                         {"hold_days": hold_days, "basis": basis})
    pub = _event_day(code, as_of)
    if pub is None:
        return _insufficient(metric_id, code, as_of, fp, {"reason": "no_event"})
    rows = _quote_rows(code, as_of)
    open_rows = [(d, opening) for d, opening, _, _ in rows
                 if d <= as_of and opening is not None]
    close_by = {d: closing for d, _, closing, _ in rows
                if d <= as_of and closing is not None}
    # 公告后首交易日:第一个 > pub 的开盘日
    first_i = next((i for i, (d, _) in enumerate(open_rows) if d > pub), None)
    first = open_rows[first_i][0] if first_i is not None else None
    if first is None:
        return _insufficient(metric_id, code, as_of, fp,
                             {"reason": "no_post_pub_day", "pub": pub.isoformat()})
    # 事件窗外(离公告超过 hold 交易日)→ None
    n_after = len(open_rows) - first_i
    if n_after > hold_days:
        return _insufficient(metric_id, code, as_of, fp,
                             {"reason": "stale", "pub": pub.isoformat()})
    o = open_rows[first_i][1]
    # 前收 = first 前一个交易日 close
    prev_c = close_by.get(open_rows[first_i - 1][0]) if first_i > 0 else None
    if o is None or not prev_c:
        return _insufficient(metric_id, code, as_of, fp,
                             {"reason": "no_px", "pub": pub.isoformat()})
    jor = (o / prev_c - 1.0) * 100.0
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=metric_id,
        raw_value=float(jor), raw_unit="percent",
        source_max_date=as_of, available_at=pub, valid=True, raw_fingerprint=fp,
        lookback_observations=n_after,
        diagnostics={"pub": pub.isoformat(), "first_day": first.isoformat()})


def compute_rec_acc_rev(code: str, as_of: date,
                        hold_days: int = JOR_HOLD_DAYS, basis: str = "qfq",
                        metric_id: str = METRIC_RECACC) -> RawFactorObservation:
    """公告后累积漂移取负(%)。窗外缺失。高=公告后跌了的(错杀) → 好。"""
    hold_days = int(hold_days)
    if hold_days <= 0:
        raise ValueError("rec_acc_rev 的 hold_days 必须为正")
    if basis != "qfq":
        raise ValueError("当前 rec_acc_rev 只支持 basis=qfq")
    fp = raw_fingerprint(metric_id, "neg_cum_ret_since_earnings",
                         {"hold_days": hold_days, "basis": basis})
    pub = _event_day(code, as_of)
    if pub is None:
        return _insufficient(metric_id, code, as_of, fp, {"reason": "no_event"})
    rows = _quote_rows(code, as_of)
    qfq_rows = [(d, closing) for d, _, _, closing in rows
                if d <= as_of and closing is not None]
    first_i = next((i for i, (d, _) in enumerate(qfq_rows) if d > pub), None)
    first = qfq_rows[first_i][0] if first_i is not None else None
    if first is None:
        return _insufficient(metric_id, code, as_of, fp,
                             {"reason": "no_post_pub_day"})
    n_after = len(qfq_rows) - first_i
    if n_after > hold_days:
        return _insufficient(metric_id, code, as_of, fp, {"reason": "stale"})
    c0, c1 = qfq_rows[first_i][1], qfq_rows[-1][1]
    if not c0 or not c1:
        return _insufficient(metric_id, code, as_of, fp, {"reason": "no_px"})
    drift = -(c1 / c0 - 1.0) * 100.0
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=metric_id,
        raw_value=float(drift), raw_unit="percent",
        source_max_date=as_of, available_at=pub, valid=True, raw_fingerprint=fp,
        lookback_observations=n_after,
        diagnostics={"pub": pub.isoformat(), "days_since": n_after})
