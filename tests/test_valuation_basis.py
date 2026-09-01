"""hfq实验口径单测；正式回测禁用原因与迁移计划见 docs/BACKTEST.md。"""
from __future__ import annotations

import unittest
from array import array
from datetime import date


def _sctx(series_map, dates):
    """series_map: {code: {col_key: [vals]}};dates: 升序列表。返回 _SeriesCtx(valid 全 1)。"""
    from stockfu.backtest.engine import _COL_KEYS, _SeriesCtx
    n = len(dates)
    series, valid = {}, {}
    for code, cols in series_map.items():
        full = {k: array("d", cols.get(k, [float("nan")] * n)) for k in _COL_KEYS}
        series[code] = full
        valid[code] = array("b", [1] * n)
    return _SeriesCtx(series=series, dates=dates,
                      date_idx={d: i for i, d in enumerate(dates)}, valid=valid)


class TestPickPx(unittest.TestCase):
    def setUp(self):
        self.bar = {"close_hfq": 11425.0, "open_hfq": 11400.0,
                    "close_raw": 1639.0, "open_raw": 1630.0,
                    "close": 1490.0, "open": 1485.0}

    def test_hfq_picks_hfq(self):
        from stockfu.backtest.engine import _pick_px
        self.assertEqual(_pick_px(self.bar, "close_hfq", "close_raw", "close", "hfq"), 11425.0)
        self.assertEqual(_pick_px(self.bar, "open_hfq", "open_raw", "open", "hfq"), 11400.0)

    def test_raw_picks_raw(self):
        from stockfu.backtest.engine import _pick_px
        self.assertEqual(_pick_px(self.bar, "close_hfq", "close_raw", "close", "raw"), 1639.0)

    def test_qfq_picks_qfq_key(self):
        from stockfu.backtest.engine import _pick_px
        # qfq 研究模式主线:优先前复权键(close/open);已含分红再投。
        self.assertEqual(_pick_px(self.bar, "close_hfq", "close_raw", "close", "qfq"), 1490.0)
        self.assertEqual(_pick_px(self.bar, "open_hfq", "open_raw", "open", "qfq"), 1485.0)

    def test_qfq_missing_falls_back_to_raw(self):
        from stockfu.backtest.engine import _pick_px
        bar = {"close_hfq": None, "open_hfq": None,
               "close_raw": 1639.0, "open_raw": None, "close": None, "open": None}
        # qfq 键缺失 → 回落 raw(qfq 与 raw 共享未除权基准,可安全回落)
        self.assertEqual(_pick_px(bar, "close_hfq", "close_raw", "close", "qfq"), 1639.0)

    def test_hfq_missing_falls_back_to_raw_then_qfq(self):
        from stockfu.backtest.engine import _pick_px
        bar = {"close_hfq": None, "open_hfq": None,
               "close_raw": 1639.0, "open_raw": None, "close": 1490.0, "open": 1485.0}
        # ETF/指数无 hfq → 回落 raw
        self.assertEqual(_pick_px(bar, "close_hfq", "close_raw", "close", "hfq"), 1639.0)
        # raw 也缺 → 回落 qfq
        self.assertEqual(_pick_px(bar, "open_hfq", "open_raw", "open", "hfq"), 1485.0)


class TestV2BasisValidation(unittest.TestCase):
    def test_invalid_basis_raises_before_any_io(self):
        # V1 run_backtest 已移除；同一契约由 V2RunConfig.__post_init__ 承接。
        from unittest.mock import MagicMock

        from stockfu.backtest.v2_engine import V2RunConfig
        with self.assertRaises(ValueError) as cm:
            V2RunConfig(
                alpha=MagicMock(), portfolio=MagicMock(), risk=MagicMock(),
                profiles={}, raw_computers={}, codes=["600001"],
                eval_start=date(2024, 1, 1), eval_end=date(2024, 1, 2),
                history_origin=date(2023, 1, 1), valuation_basis="bogus",
            )
        self.assertIn("valuation_basis", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
