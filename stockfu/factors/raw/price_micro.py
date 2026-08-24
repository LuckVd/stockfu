"""price_micro:量价微观结构因子族(隔夜/日内/CGO/Amihud/理想反转W切割)。

2026-08-18 接入,IC 快验判别见 scripts/price_micro_ic.py 与
data/backtest/price-micro-ic-full.txt(月频截面,full/2013-2019/2020-2026 三段):

- ``overnight_ret_20d``  近 20 日隔夜收益均值(raw open/前收 raw)。T+1 隔夜折价
  异象;IC 弱(+0.009@1d/两段偏正),保留作合成维度。
- ``intraday_ret_20d``   近 20 日日内收益均值。两段 IC 一致为负(-0.02~-0.04):
  日内涨幅大=散户追涨过度,未来跑输;direction=lower_is_better。
- ``cgo_60d``            资本利得突出量(换手加权参考价递归,Grinblatt-Han 处置
  效应)。IC:full -0.052@5d(ICIR -3.0),两子段一致负 → 低 CGO(深度浮亏、
  卖压出尽)跑赢;direction=lower_is_better。
- ``amihud_20d``         Amihud 非流动性均值(|ret|/amount)。2013-19 IC +0.033~
  +0.046(强),2020+ 衰减至 0;流动性溢价在大盘池 2020 后被机构定价;
  direction=higher_is_better,权重放低。
- ``wsplit_rev_20d``     理想反转 W 切割(东吴"订单簿的温度"):按单笔成交金额
  代理 D=amount/volume 把 20 日切成高 D/低 D 两组,因子=高D组收益和-低D组收益和。
  无成交笔数列,D 用元/手近似。IC:2020+ -0.032@5d(ICIR -3.0)、2013-19 弱;
  高 D(聪明钱)推动的涨幅是"假反转"会回吐 → direction=lower_is_better。

口径:全部只用 <= as_of 数据;隔夜/日内用 raw 开收(复权无意义),CGO 用 qfq
close + 原始换手率(%),Amihud 用 raw close 收益 + amount(元)。
"""
from __future__ import annotations

from datetime import date

from stockfu.factors.raw import raw_fingerprint
from stockfu.scoring.contracts import MissingReason, RawFactorObservation

METRIC_OVERNIGHT = "overnight_ret_20d"
METRIC_INTRADAY = "intraday_ret_20d"
METRIC_CGO = "cgo_60d"
METRIC_AMIHUD = "amihud_20d"
METRIC_WSPLIT = "wsplit_rev_20d"

MIN_VALID = 15


def _insufficient(metric_id: str, code: str, as_of: date, fp: str,
                  diag: dict) -> RawFactorObservation:
    unit = {"amihud_20d": "per_1e8_cny_x1000"}.get(metric_id, "percent")
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=metric_id,
        raw_value=None, raw_unit=unit, source_max_date=as_of,
        available_at=as_of, valid=False,
        missing_reason=MissingReason.INSUFFICIENT_SAMPLES, raw_fingerprint=fp,
        diagnostics=diag)


def compute_overnight_ret(code: str, as_of: date, window: int = 20,
                          basis: str = "raw",
                          metric_id: str = METRIC_OVERNIGHT) -> RawFactorObservation:
    """近 N 日隔夜收益均值(%):mean(open_raw/prev_close_raw - 1)。"""
    window = int(window)
    if window <= 0:
        raise ValueError("overnight_ret_20d 的 window 必须为正")
    if basis != "raw":
        raise ValueError("当前 overnight_ret_20d 只支持 basis=raw")
    fp = raw_fingerprint(metric_id, "mean_overnight_pct",
                         {"window": window, "basis": basis})
    from stockfu.services.factors import quote_series_dates

    span = int(window * 1.5) + 30
    d_open, opens = quote_series_dates(code, "open", span, as_of=as_of, adj="raw")
    d_close, closes = quote_series_dates(code, "close", span, as_of=as_of, adj="raw")
    close_by = dict(zip(d_close, closes))
    on_rets: list[float] = []
    for d, o in zip(d_open, opens):
        prev = close_by.get(_prev_day(d_open, d))
        if prev and o:
            on_rets.append((o / prev - 1.0) * 100.0)
    if len(on_rets) < MIN_VALID:
        return _insufficient(metric_id, code, as_of, fp, {"n": len(on_rets)})
    recent = on_rets[-window:]
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=metric_id,
        raw_value=float(sum(recent) / len(recent)), raw_unit="percent",
        source_max_date=as_of, available_at=as_of, valid=True, raw_fingerprint=fp,
        lookback_observations=len(recent),
        diagnostics={"window_used": len(recent)})


def _prev_day(days: list[date], d: date) -> date | None:
    idx = {x: i for i, x in enumerate(days)}
    i = idx.get(d)
    if i is None or i == 0:
        return None
    return days[i - 1]


def compute_intraday_ret(code: str, as_of: date, window: int = 20,
                         basis: str = "raw",
                         metric_id: str = METRIC_INTRADAY) -> RawFactorObservation:
    """近 N 日日内收益均值(%):mean((close_raw-open_raw)/prev_close_raw)。"""
    window = int(window)
    if window <= 0:
        raise ValueError("intraday_ret_20d 的 window 必须为正")
    if basis != "raw":
        raise ValueError("当前 intraday_ret_20d 只支持 basis=raw")
    fp = raw_fingerprint(metric_id, "mean_intraday_pct",
                         {"window": window, "basis": basis})
    from stockfu.services.factors import quote_series_dates

    span = int(window * 1.5) + 30
    d_open, opens = quote_series_dates(code, "open", span, as_of=as_of, adj="raw")
    d_close, closes = quote_series_dates(code, "close", span, as_of=as_of, adj="raw")
    open_by = dict(zip(d_open, opens))
    on_rets, in_rets = [], []
    for i in range(1, len(d_close)):
        d = d_close[i]
        prev_c = closes[i - 1]
        o = open_by.get(d)
        c = closes[i]
        if prev_c and o is not None and c is not None:
            on = o / prev_c - 1.0
            on_rets.append(on)
            in_rets.append(c / prev_c - 1.0 - on)
    if len(in_rets) < MIN_VALID:
        return _insufficient(metric_id, code, as_of, fp, {"n": len(in_rets)})
    recent = [r * 100.0 for r in in_rets[-window:]]
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=metric_id,
        raw_value=float(sum(recent) / len(recent)), raw_unit="percent",
        source_max_date=as_of, available_at=as_of, valid=True, raw_fingerprint=fp,
        lookback_observations=len(recent),
        diagnostics={"window_used": len(recent), "n_overnight": len(on_rets)})


def compute_cgo(code: str, as_of: date, span: int = 60,
                basis: str = "qfq_close_raw_turnover",
                metric_id: str = METRIC_CGO) -> RawFactorObservation:
    """资本利得突出量:close/换手加权参考价 - 1(%),span 交易日递归。

    RP_t = (1-k_t)*RP_{t-1} + k_t*P_t,k=换手率小数(cap 0.99);
    CGO = P_t/RP_t - 1。低 CGO=筹码深套、抛压出尽(处置效应卖盈持亏 →
    浮盈盘未来供给大)。原始换手率 turnover(%)不复权直接用。
    """
    span = int(span)
    if span <= 0:
        raise ValueError("cgo_60d 的 span 必须为正")
    if basis != "qfq_close_raw_turnover":
        raise ValueError("当前 cgo_60d 只支持 basis=qfq_close_raw_turnover")
    fp = raw_fingerprint(metric_id, "turnover_weighted_reference_price",
                         {"span": span, "basis": basis})
    from stockfu.services.factors import quote_series

    cal = int(span * 1.5) + 30
    closes = quote_series(code, "close", cal, as_of=as_of)
    turns = quote_series(code, "turnover", cal, as_of=as_of)
    if len(closes) < MIN_VALID or not turns:
        return _insufficient(metric_id, code, as_of, fp,
                             {"n_closes": len(closes), "n_turns": len(turns)})
    n = min(len(closes), len(turns), span)
    closes_r, turns_r = closes[-n:], turns[-n:]
    rp = None
    for c, t in zip(closes_r, turns_r):
        k = min(max((t or 0.0) / 100.0, 0.0), 0.99)
        rp = c if rp is None else (1.0 - k) * rp + k * c
    last = closes_r[-1]
    if not rp or not last:
        return _insufficient(metric_id, code, as_of, fp, {"rp": rp, "last": last})
    cgo = (last / rp - 1.0) * 100.0
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=metric_id,
        raw_value=float(cgo), raw_unit="percent",
        source_max_date=as_of, available_at=as_of, valid=True, raw_fingerprint=fp,
        lookback_observations=n,
        diagnostics={"span_used": n, "rp": round(rp, 4)})


def compute_amihud(code: str, as_of: date, window: int = 20,
                   basis: str = "raw", amount_unit: str = "1e8",
                   metric_id: str = METRIC_AMIHUD) -> RawFactorObservation:
    """Amihud 非流动性:mean(|ret_raw| / amount_亿元),×1000 便于读数。"""
    window = int(window)
    if window <= 0:
        raise ValueError("amihud_20d 的 window 必须为正")
    if basis != "raw" or amount_unit != "1e8":
        raise ValueError("当前 amihud_20d 只支持 basis=raw、amount_unit=1e8")
    fp = raw_fingerprint(metric_id, "mean_absret_over_amount",
                         {"window": window, "basis": basis, "amount_unit": amount_unit})
    from stockfu.services.factors import quote_series

    span = int(window * 1.5) + 30
    closes = quote_series(code, "close", span, as_of=as_of, adj="raw")
    amounts = quote_series(code, "amount", span, as_of=as_of)
    if len(closes) < MIN_VALID or not amounts:
        return _insufficient(metric_id, code, as_of, fp,
                             {"n_closes": len(closes), "n_amounts": len(amounts)})
    n = min(len(closes) - 1, len(amounts) - 1)
    vals: list[float] = []
    for i in range(1, n + 1):
        c0, c1, amt = closes[i - 1], closes[i], amounts[i]
        if c0 and c1 is not None and amt:
            vals.append(abs(c1 / c0 - 1.0) / (amt / 1e8) * 1000.0)
    if len(vals) < MIN_VALID:
        return _insufficient(metric_id, code, as_of, fp, {"n": len(vals)})
    recent = vals[-window:]
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=metric_id,
        raw_value=float(sum(recent) / len(recent)), raw_unit="per_1e8_cny_x1000",
        source_max_date=as_of, available_at=as_of, valid=True, raw_fingerprint=fp,
        lookback_observations=len(recent),
        diagnostics={"window_used": len(recent)})


def compute_wsplit_rev(code: str, as_of: date, window: int = 20,
                       basis: str = "raw", D: str = "amount/volume",
                       metric_id: str = METRIC_WSPLIT) -> RawFactorObservation:
    """理想反转 W 切割:高单笔金额日收益和 - 低单笔金额日收益和(%)。

    D=amount/volume(元/手,单笔强度代理);高 D 日=聪明钱主导。
    因子高=高 D 日涨得多(假反转,聪明钱已在推高)→ 未来跑输,lower_is_better。
    """
    window = int(window)
    if window <= 0:
        raise ValueError("wsplit_rev_20d 的 window 必须为正")
    if basis != "raw" or D != "amount/volume":
        raise ValueError("当前 wsplit_rev_20d 只支持 basis=raw、D=amount/volume")
    fp = raw_fingerprint(metric_id, "w_split_by_amount_per_volume",
                         {"window": window, "basis": basis, "D": D})
    from stockfu.services.factors import quote_series

    span = int(window * 1.5) + 30
    closes = quote_series(code, "close", span, as_of=as_of, adj="raw")
    volumes = quote_series(code, "volume", span, as_of=as_of)
    amounts = quote_series(code, "amount", span, as_of=as_of)
    if len(closes) < MIN_VALID or not volumes or not amounts:
        return _insufficient(metric_id, code, as_of, fp,
                             {"n_closes": len(closes)})
    n = min(len(closes) - 1, len(volumes) - 1, len(amounts) - 1)
    rows: list[tuple[float, float]] = []
    for i in range(1, n + 1):
        c0, c1 = closes[i - 1], closes[i]
        vol, amt = volumes[i], amounts[i]
        if c0 and c1 is not None and vol and amt:
            rows.append((amt / vol, (c1 / c0 - 1.0) * 100.0))
    if len(rows) < MIN_VALID:
        return _insufficient(metric_id, code, as_of, fp, {"n": len(rows)})
    rows = rows[-window:]
    rows.sort(key=lambda x: -x[0])
    half = len(rows) // 2
    hi = sum(r for _, r in rows[:half])
    lo = sum(r for _, r in rows[half:])
    return RawFactorObservation(
        asset_code=code, as_of=as_of, raw_metric_id=metric_id,
        raw_value=float(hi - lo), raw_unit="percent",
        source_max_date=as_of, available_at=as_of, valid=True, raw_fingerprint=fp,
        lookback_observations=len(rows),
        diagnostics={"window_used": len(rows), "hi_sum": round(hi, 4),
                     "lo_sum": round(lo, 4)})
