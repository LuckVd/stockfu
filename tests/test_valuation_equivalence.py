"""valuation_snapshot 向量化改造的等价黄金值基线。

provide_valuation(engine.py) + valuation_snapshot(services/valuation.py) 是 V1/V2
共享代码。本测试锁死 6 组覆盖全部隐式行为的边界输出,作为任何向量化/重构的安全网:
  ① 当天值(rows[-1]) ② nan 过滤 ③ nan->None 映射 ④ ETF/指数全 nan 回退
  ⑤ pe/pb<=0 守卫 ⑥ 样本<10 时 med/p25/p75=None。

改造前后本测试必须逐字段全过;任何一行变红 = 实现偏离 = 回测分位/选股会变。
"""
from __future__ import annotations

import unittest
from array import array
from datetime import date, timedelta

NAN = float("nan")


def _mk(code, rows):
    """rows: list[(close, pe, pb)], None->nan, 连续历日。"""
    n = len(rows)
    start = date(2020, 1, 2)
    dates = [start + timedelta(days=i) for i in range(n)]
    from stockfu.backtest.engine import _COL_KEYS, _SeriesCtx

    series = {code: {k: array("d", [NAN] * n) for k in _COL_KEYS}}
    valid = {code: array("b", [0] * n)}
    for i, (c, p, b) in enumerate(rows):
        series[code]["c"][i] = c if c is not None else NAN
        series[code]["pe"][i] = p if p is not None else NAN
        series[code]["pb"][i] = b if b is not None else NAN
        valid[code][i] = 1
    return _SeriesCtx(series=series, dates=dates,
                      date_idx={d: i for i, d in enumerate(dates)}, valid=valid)


def _snap(code, rows, as_of_off, years=5):
    """构造 ctx 并取最后一天(或指定偏移)的 valuation_snapshot 全字段。"""
    from stockfu.backtest.engine import _backtest_series_ctx
    from stockfu.services.valuation import valuation_snapshot

    sctx = _mk(code, rows)
    with _backtest_series_ctx(sctx):
        return valuation_snapshot(code, sctx.dates[as_of_off], years=years)


class TestValuationEquivalence(unittest.TestCase):
    FIELDS = ("pe", "pb", "pe_pct", "pb_pct", "n_pe", "n_pb",
              "pe_med", "pe_p25", "pe_p75", "pb_med", "pb_p25", "pb_p75",
              "value_zone")

    def _assert(self, snap, expected):
        for k in self.FIELDS:
            v = snap[k]
            e = expected[k]
            if e is None:
                self.assertIsNone(v, f"{k} 应为 None,实为 {v!r}")
            elif isinstance(e, float):
                self.assertAlmostEqual(v, e, places=2, msg=f"{k}: {v} != {e}")
            else:
                self.assertEqual(v, e, f"{k}: {v} != {e}")

    def test_normal_large(self):
        rows = [(10.0, float(i + 1), float(i + 2)) for i in range(1300)]
        self._assert(_snap("600001", rows, 1299), {
            "pe": 1300.0, "pb": 1301.0, "pe_pct": 99.96, "pb_pct": 99.96,
            "n_pe": 1300, "n_pb": 1300,
            "pe_med": 650.5, "pe_p25": 325.75, "pe_p75": 975.25,
            "pb_med": 651.5, "pb_p25": 326.75, "pb_p75": 976.25,
            "value_zone": "rich",
        })

    def test_partial_nan_head(self):
        rows = [(10.0, NAN, NAN)] * 300 + [(10.0, float(i + 1), float(i + 2)) for i in range(1000)]
        self._assert(_snap("600002", rows, 1299), {
            "pe": 1000.0, "pb": 1001.0, "pe_pct": 99.95, "pb_pct": 99.95,
            "n_pe": 1000, "n_pb": 1000,
            "pe_med": 500.5, "pe_p25": 250.75, "pe_p75": 750.25,
            "pb_med": 501.5, "pb_p25": 251.75, "pb_p75": 751.25,
            "value_zone": "rich",
        })

    def test_interspersed_nan(self):
        rows = [(10.0, (float(i) if i % 3 else NAN), (float(i + 1) if i % 3 else NAN)) for i in range(800)]
        self._assert(_snap("600003", rows, 799), {
            "pe": 799.0, "pb": 800.0, "pe_pct": 99.91, "pb_pct": 99.91,
            "n_pe": 533, "n_pb": 533,
            "pe_med": 400.0, "pe_p25": 200.0, "pe_p75": 599.0,
            "pb_med": 401.0, "pb_p25": 201.0, "pb_p75": 600.0,
            "value_zone": "rich",
        })

    def test_small_sample_lt10(self):
        rows = [(10.0, float(i + 1), float(i + 2)) for i in range(7)]
        self._assert(_snap("600004", rows, 6), {
            "pe": 7.0, "pb": 8.0, "pe_pct": None, "pb_pct": None,
            "n_pe": 7, "n_pb": 7,
            "pe_med": None, "pe_p25": None, "pe_p75": None,
            "pb_med": None, "pb_p25": None, "pb_p75": None,
            "value_zone": "unknown",
        })

    def test_full_nan_etf_fallback(self):
        rows = [(10.0, NAN, NAN)] * 1200
        self._assert(_snap("510300", rows, 1199), {
            "pe": None, "pb": None, "pe_pct": None, "pb_pct": None,
            "n_pe": 0, "n_pb": 0,
            "pe_med": None, "pe_p25": None, "pe_p75": None,
            "pb_med": None, "pb_p25": None, "pb_p75": None,
            "value_zone": "unknown",
        })

    def test_pe_nonpositive_guard(self):
        rows = [(10.0, (float(i + 1) if i < 600 else -1.0), float(i + 2)) for i in range(1000)]
        self._assert(_snap("600006", rows, 999), {
            "pe": None, "pb": 1001.0, "pe_pct": None, "pb_pct": 99.95,
            "n_pe": 600, "n_pb": 1000,
            "pe_med": 300.5, "pe_p25": 150.75, "pe_p75": 450.25,
            "pb_med": 501.5, "pb_p25": 251.75, "pb_p75": 751.25,
            "value_zone": "rich",
        })


if __name__ == "__main__":
    unittest.main()
