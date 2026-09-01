"""回测数据供给器：TTM 分红不得在热路径逐次查库。"""
from __future__ import annotations

import unittest
from array import array
from datetime import date
from unittest.mock import patch


def _sctx_with_close(code, close_by_day, *, raw_too=True):
    """构造列式 _SeriesCtx:code 在指定日期上有 close(qfq),可选同步写 close_raw。"""
    from stockfu.backtest.engine import _COL_KEYS, _SeriesCtx
    NAN = float("nan")
    dates = sorted(close_by_day)
    n = len(dates)
    series = {code: {k: array("d", [NAN] * n) for k in _COL_KEYS}}
    valid = {code: array("b", [0] * n)}
    for i, d in enumerate(dates):
        v = float(close_by_day[d])
        series[code]["c"][i] = v
        if raw_too:
            series[code]["c_raw"][i] = v
        valid[code][i] = 1
    return _SeriesCtx(series=series, dates=dates,
                      date_idx={d: i for i, d in enumerate(dates)}, valid=valid)


class TestBacktestDataProviders(unittest.TestCase):
    def test_dividend_yield_uses_injected_events(self):
        from stockfu.services.dividend import (
            clear_backtest_dividend_provider,
            dividend_yield_ttm,
            set_backtest_dividend_provider,
        )

        try:
            set_backtest_dividend_provider(
                lambda code, start, end: [(date(2024, 1, 10), 0.5)] if code == "600001" else None)
            # raw 价格仍由行情供给器负责；此处 mock 只验证分红事件未触库。
            with patch("stockfu.services.factors.quote_series", return_value=[10.0]):
                self.assertEqual(dividend_yield_ttm("600001", date(2024, 6, 1)), (5.0, 0.5))
        finally:
            clear_backtest_dividend_provider()

    def test_dividend_yield_uses_raw_price_preload(self):
        from stockfu.backtest.engine import _backtest_series_ctx
        from stockfu.services.dividend import dividend_yield_ttm

        code = "600001"
        as_of = date(2024, 6, 1)
        sctx = _sctx_with_close(code, {as_of: 10})   # close_raw=10 → 分母
        dividends = {code: [(date(2024, 1, 10), 0.5)]}
        with _backtest_series_ctx(sctx, dividends):
            self.assertEqual(dividend_yield_ttm(code, as_of), (5.0, 0.5))


if __name__ == "__main__":
    unittest.main()
