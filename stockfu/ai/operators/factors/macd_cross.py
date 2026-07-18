"""MACD 金叉/死叉算子: 日线 + 周线双维度交叉检测。

从 quote_snapshot 读日线 close → 算日/周 MACD → 组合评分。
现有 LLM tool(macd.py) 的 EMA 计算逻辑复用于此。
"""
from datetime import date, timedelta

from sqlmodel import select

from stockfu.ai.operators.base import BaseOperator, OpResult
from stockfu.ai.operators.registry import register
from stockfu.db import session_scope
from stockfu.models import QuoteSnapshot
from stockfu.services.factors import quote_model_for, quote_series


# ── EMA 计算 ──
def _ema(data: list[float], period: int) -> list[float | None]:
    if len(data) < period:
        return [None] * len(data)
    alpha = 2 / (period + 1)
    ema_vals: list[float] = [sum(data[:period]) / period]
    for i in range(period, len(data)):
        ema_vals.append(ema_vals[-1] * (1 - alpha) + data[i] * alpha)
    return [None] * (period - 1) + ema_vals


def _macd_series(closes: list[float], fast: int, slow: int, signal: int):
    """计算 MACD 三线, 返回 (dif_list, dea_list, hist_list) 与最新值对齐。"""
    if len(closes) < slow + signal:
        return [], [], []
    ema_f = _ema(closes, fast)
    ema_s = _ema(closes, slow)
    dif = [f - s if f is not None and s is not None else None
           for f, s in zip(ema_f, ema_s)]
    dif_filtered = [x for x in dif if x is not None]
    if len(dif_filtered) < signal:
        return dif, [], []
    dea_raw = _ema(dif_filtered, signal)
    # pad DEA 到与 dif 等长
    pad = len(dif) - len(dif_filtered)
    dea: list[float | None] = ([None] * pad + [None] * (len(dif_filtered) - len(dea_raw))
                                + dea_raw)
    hist = [(d - dea[i]) if d is not None and dea[i] is not None else None
            for i, d in enumerate(dif)]
    return dif, dea, hist


def _check_cross(dif: list[float | None], dea: list[float | None]) -> str:
    """检查最新两根 MACD 线的交叉状态。返回 'golden' / 'death' / 'none'。"""
    if len(dif) < 2 or len(dea) < 2:
        return "none"
    d2, d1 = dif[-2], dif[-1]
    e2, e1 = dea[-2], dea[-1]
    if d2 is None or d1 is None or e2 is None or e1 is None:
        return "none"
    if d2 <= e2 and d1 > e1:
        return "golden"
    if d2 >= e2 and d1 < e1:
        return "death"
    return "none"


def _weekly_closes(code: str, as_of: date | None, lookback_days: int) -> list[float]:
    """按周聚合收盘价: 取每周最后一个交易日的 close。"""
    ref = as_of or date.today()
    start = ref - timedelta(days=lookback_days)
    model = quote_model_for(code)
    with session_scope() as s:
        rows = s.exec(
            select(model).where(
                model.asset_code == code,
                model.quote_date >= start,
                model.quote_date <= ref,
            ).order_by(model.quote_date)
        ).all()
    # 按 ISO 周聚合,取每周最后一条
    weekly: dict[tuple[int, int], float] = {}  # (year, week) -> close
    for r in rows:
        d = r.quote_date if hasattr(r, "quote_date") else r.snap_date
        iso = d.isocalendar()
        weekly[(iso[0], iso[1])] = getattr(r, "close", None) or 0.0
    return list(weekly.values())


# ── 算子 ──
@register
class MacdCrossOperator(BaseOperator):
    operator_id = "macd_cross"
    type = "math"
    PARAMS_SCHEMA = {"fast": 12, "slow": 26, "signal": 9}

    def run(self, ctx, params):
        fast = int(params.get("fast", 12))
        slow = int(params.get("slow", 26))
        signal = int(params.get("signal", 9))
        need = slow + signal + 60  # 额外取足周线数据

        # --- 日线 MACD ---
        closes = quote_series(ctx.code, "close", need, as_of=ctx.as_of)
        if len(closes) < slow + signal:
            return OpResult(
                operator=self.operator_id, type="math", value=None,
                signal="hold", score=0.0, confidence=0.3,
                reasoning=f"日线数据不足({len(closes)}<{slow + signal})",
            )
        dif_d, dea_d, _ = _macd_series(closes, fast, slow, signal)
        daily_cross = _check_cross(dif_d, dea_d)

        # --- 周线 MACD ---
        weekly = _weekly_closes(ctx.code, ctx.as_of, need)
        if len(weekly) < slow + signal:
            # 周线数据不足 → 只用日线
            weekly_cross = "none"
        else:
            dif_w, dea_w, _ = _macd_series(weekly, fast, slow, signal)
            weekly_cross = _check_cross(dif_w, dea_w)

        # --- 信号组合评分 ---
        # 冲突: 日金叉 + 周死叉(或反之) → 无信号
        if daily_cross == "none" and weekly_cross == "none":
            score, signal_out, reason = 0.0, "hold", "无金叉/死叉"
        elif daily_cross == "golden" and weekly_cross == "golden":
            score, signal_out = 10.0, "strong_buy"
            reason = "日线金叉 + 周线金叉"
        elif daily_cross == "golden" and weekly_cross == "none":
            score, signal_out = 5.0, "buy"
            reason = "日线金叉(周线无交叉)"
        elif daily_cross == "none" and weekly_cross == "golden":
            score, signal_out = 7.0, "buy"
            reason = "周线金叉(日线无交叉)"
        elif daily_cross == "death" and weekly_cross == "death":
            score, signal_out = -10.0, "strong_sell"
            reason = "日线死叉 + 周线死叉"
        elif daily_cross == "death" and weekly_cross == "none":
            score, signal_out = -5.0, "sell"
            reason = "日线死叉(周线无交叉)"
        elif daily_cross == "none" and weekly_cross == "death":
            score, signal_out = -7.0, "sell"
            reason = "周线死叉(日线无交叉)"
        else:  # 冲突: 日金叉+周死叉 或 日死叉+周金叉
            score, signal_out = 0.0, "hold"
            direction = "周线金叉" if weekly_cross == "golden" else "周线死叉"
            daily_dir = "日线金叉" if daily_cross == "golden" else "日线死叉"
            reason = f"冲突: {daily_dir}+{direction}, 观望"
            confidence = 0.3
            return OpResult(operator=self.operator_id, type="math",
                            value=None, signal=signal_out, score=score,
                            confidence=confidence, reasoning=reason)

        # 最新 DIF, DEA 作为 value 输出
        latest_dif = round(dif_d[-1], 4) if dif_d and dif_d[-1] is not None else None
        return OpResult(
            operator=self.operator_id, type="math",
            value=latest_dif,
            signal=signal_out, score=score, confidence=0.7,
            reasoning=f"日常规({daily_cross}) 周线({weekly_cross}) → {reason}",
        )
