"""value 算子回测预载：估值窗口必须走内存、且不改变分位口径。"""
from __future__ import annotations

import unittest
from array import array
from datetime import date, timedelta


def _sctx_with_pe_pb(code, start, n_days):
    """构造列式 _SeriesCtx:code 在 n_days 个连续历日上有 close/pe/pb。"""
    from stockfu.backtest.engine import _COL_KEYS, _SeriesCtx
    NAN = float("nan")
    dates = [start + timedelta(days=i) for i in range(n_days)]
    series = {code: {k: array("d", [NAN] * n_days) for k in _COL_KEYS}}
    valid = {code: array("b", [0] * n_days)}
    for i in range(n_days):
        series[code]["c"][i] = 10.0          # close(qfq) —— valuation 读取
        series[code]["pe"][i] = float(i + 1)
        series[code]["pb"][i] = float(i + 2)
        valid[code][i] = 1
    return _SeriesCtx(series=series, dates=dates,
                      date_idx={d: i for i, d in enumerate(dates)}, valid=valid)


class TestValuationPreload(unittest.TestCase):
    def test_valuation_snapshot_uses_preloaded_pe_pb(self):
        from stockfu.backtest.engine import _backtest_series_ctx
        from stockfu.services.valuation import valuation_snapshot

        code = "600001"
        start = date(2024, 1, 2)
        sctx = _sctx_with_pe_pb(code, start, 10)

        with _backtest_series_ctx(sctx):
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
