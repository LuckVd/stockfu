"""value 算子回测预载：估值窗口必须走内存、且不改变分位口径。"""
from __future__ import annotations

import unittest
from datetime import date, timedelta


class TestValuationPreload(unittest.TestCase):
    def test_valuation_snapshot_uses_preloaded_pe_pb(self):
        from stockfu.backtest.engine import _backtest_series_ctx
        from stockfu.services.valuation import valuation_snapshot

        code = "600001"
        start = date(2024, 1, 2)
        cache = {}
        for i in range(10):
            day = start + timedelta(days=i)
            # (open, high, low, close, pct, st, trade_status, amount, pe, pb)
            cache[day] = {code: (10.0, 10.0, 10.0, 10.0, 0.0, 0, 1,
                                 1_000_000.0, float(i + 1), float(i + 2))}

        with _backtest_series_ctx(cache):
            snap = valuation_snapshot(code, start + timedelta(days=9), years=1)

        self.assertEqual(snap["pe"], 10.0)
        self.assertEqual(snap["pb"], 11.0)
        self.assertEqual(snap["n_pe"], 10)
        self.assertEqual(snap["n_pb"], 10)
        self.assertAlmostEqual(snap["pe_pct"], 95.0)
        self.assertAlmostEqual(snap["pb_pct"], 95.0)

    def test_sorted_percentile_matches_public_helper(self):
        from stockfu.services.factors import percentile
        from stockfu.services.valuation import _percentile_sorted

        series = [1.0, 2.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]
        for value in (2.0, 5.0, 9.0, None):
            self.assertEqual(_percentile_sorted(series, value), percentile(series, value)[0])


if __name__ == "__main__":
    unittest.main()
