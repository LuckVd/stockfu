"""lhb:龙虎榜事件因子(近 N 日聚合,无事件日=有效零值)。

PIT:榜单盘后披露,lhb_date 当日可见、T+1 可交易;raw 只读 lhb_date <= as_of
即天然防未来(引擎 T+1 开盘成交)。事件稀疏(每日 ~50 只)→ 无事件日必须计为
有效零值而非缺失,否则 missing 率爆表(见 docs/SPECS/lhb-precheck-2026.md)。

- ``lhb_net_buy_20d``:近 N 日龙虎榜净买额占总成交比之和(标准化,跨票可比),
  高=大额净买(IC 快验:2024-2026 大额净买组 20 日 +0.54% vs 净卖 -3.24%)。
- ``lhb_inst_net_20d``:近 N 日机构净家数之和(解读解析,买入-卖出),
  高=机构净买(20 日价差 +1.8pct,弱于净买额信号)。
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlmodel import select

from stockfu.db import session_scope
from stockfu.factors.raw import raw_fingerprint
from stockfu.models import LhbEvent
from stockfu.scoring.contracts import RawFactorObservation

_ALGO = "sum_last_n_days"


def _lhb_rows(code: str, as_of: date, days: int) -> list[LhbEvent]:
    start = as_of - timedelta(days=int(days * 1.5) + 30)
    with session_scope() as s:
        return list(s.exec(
            select(LhbEvent).where(
                LhbEvent.asset_code == code,
                LhbEvent.lhb_date >= start,
                LhbEvent.lhb_date <= as_of,
            )
        ).all())


def compute_lhb_net_buy(code: str, as_of: date, window: int = 20,
                        metric_id: str = "lhb_net_buy",
                        ) -> RawFactorObservation:
    window = int(window)
    if window <= 0:
        raise ValueError("lhb_net_buy 的 window 必须为正")
    fp = raw_fingerprint(metric_id, _ALGO, {"window": window})
    rows = _lhb_rows(code, as_of, window)
    # 同一票同一日可多原因上榜(多条):按日去重取当日合计,再跨日求和
    by_day: dict[date, float] = {}
    for r in rows:
        if r.net_ratio is None:
            continue
        by_day[r.lhb_date] = by_day.get(r.lhb_date, 0.0) + float(r.net_ratio)
    value = round(sum(by_day.values()), 4)
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=metric_id,
        raw_value=value, raw_unit="sum_net_ratio_pct",
        source_max_date=as_of, available_at=as_of, valid=True, raw_fingerprint=fp,
        lookback_observations=len(by_day),
        diagnostics={"window": window, "event_days": len(by_day), "sum_net_ratio": value})


def compute_lhb_inst_net(code: str, as_of: date, window: int = 20,
                         metric_id: str = "lhb_inst_net",
                         ) -> RawFactorObservation:
    window = int(window)
    if window <= 0:
        raise ValueError("lhb_inst_net 的 window 必须为正")
    fp = raw_fingerprint(metric_id, _ALGO, {"window": window})
    rows = _lhb_rows(code, as_of, window)
    by_day: dict[date, int] = {}
    for r in rows:
        by_day[r.lhb_date] = by_day.get(r.lhb_date, 0) \
            + int(r.inst_buy_count or 0) - int(r.inst_sell_count or 0)
    value = round(float(sum(by_day.values())), 4)
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=metric_id,
        raw_value=value, raw_unit="sum_inst_net_count",
        source_max_date=as_of, available_at=as_of, valid=True, raw_fingerprint=fp,
        lookback_observations=len(by_day),
        diagnostics={"window": window, "event_days": len(by_day), "inst_net": value})
