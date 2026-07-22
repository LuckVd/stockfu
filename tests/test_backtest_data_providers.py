"""回测数据供给器：MACD 周线与 TTM 分红不得在热路径逐次查库。"""
from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch


class TestBacktestDataProviders(unittest.TestCase):
    def test_macd_weekly_closes_uses_market_preload(self):
        from stockfu.ai.operators.factors.macd_cross import _weekly_closes
        from stockfu.backtest.engine import _backtest_series_ctx

        code = "600001"
        # 周五收盘应覆盖同周前面的交易日。
        cache = {
            date(2024, 1, 4): {code: (1, 1, 1, 10, 0, 0, 1, 1, 10, None, None)},
            date(2024, 1, 5): {code: (1, 1, 1, 11, 0, 0, 1, 1, 11, None, None)},
            date(2024, 1, 8): {code: (1, 1, 1, 12, 0, 0, 1, 1, 12, None, None)},
            date(2024, 1, 12): {code: (1, 1, 1, 13, 0, 0, 1, 1, 13, None, None)},
        }
        with _backtest_series_ctx(cache):
            self.assertEqual(_weekly_closes(code, date(2024, 1, 12), 30), [11, 13])

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
        cache = {as_of: {code: (1, 1, 1, 9, 0, 0, 1, 1, 10, None, None)}}
        dividends = {code: [(date(2024, 1, 10), 0.5)]}
        with _backtest_series_ctx(cache, dividends):
            self.assertEqual(dividend_yield_ttm(code, as_of), (5.0, 0.5))


if __name__ == "__main__":
    unittest.main()
