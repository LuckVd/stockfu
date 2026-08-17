"""指标基准口径解析回归（2026-08-17 审查 #5）。

策略净值 qfq 含分红再投,对照价格指数会把 excess 系统性高估约基准股息率
(~2%/年)。``_resolve_metrics_benchmark`` 默认自动改用全收益孪生
(``{bench}_tr``),缺失/起点不足回退价格指数并标注 basis;显式覆盖优先。
交易行为(风险 overlay)不受影响——本测试同时钉住这一点所需的解析契约。
"""
from __future__ import annotations

import math
import unittest
from dataclasses import dataclass
from datetime import date
from types import SimpleNamespace

from stockfu.backtest.v2_engine import _resolve_metrics_benchmark


def _sctx(closes_by_code: dict[str, list[float]],
          dates: list[date]) -> SimpleNamespace:
    """合成列式 sctx:closes 含 NaN 洞;date_idx 按日期映射列。"""
    series = {}
    for code, closes in closes_by_code.items():
        cols = {"c": [float(c) if c is not None else math.nan for c in closes]}
        series[code] = cols
    return SimpleNamespace(series=series,
                           date_idx={d: i for i, d in enumerate(dates)})


@dataclass
class _Cfg:
    metrics_benchmark_code: str | None = None


class TestResolveMetricsBenchmark(unittest.TestCase):
    def setUp(self):
        self.dates = [date(2024, 1, 1 + i) for i in range(5)]

    def test_tr_present_and_covers_formal_start(self):
        sctx = _sctx({"sh000300_tr": [100, 101, 102, 103, 104]}, self.dates)
        curve, code, basis, note = _resolve_metrics_benchmark(
            sctx, _Cfg(), "sh000300", self.dates)
        self.assertEqual(code, "sh000300_tr")
        self.assertEqual(basis, "total_return")
        self.assertIsNone(note)
        self.assertEqual(curve[0]["date"], self.dates[0])
        self.assertAlmostEqual(curve[-1]["equity"], 104 / 100)

    def test_tr_missing_falls_back_to_price(self):
        sctx = _sctx({"sh000300": [10, 11, 12, 13, 14]}, self.dates)
        curve, code, basis, note = _resolve_metrics_benchmark(
            sctx, _Cfg(), "sh000300", self.dates)
        self.assertEqual((code, basis, note), ("sh000300", "price", "tr_missing"))
        self.assertAlmostEqual(curve[-1]["equity"], 14 / 10)

    def test_tr_late_start_falls_back_to_price(self):
        # TR 曲线首点晚于 formal 起点(窗口错位)→ 宁可回退价格指数
        sctx = _sctx({
            "sh000300_tr": [None, None, 100, 101, 102],
            "sh000300": [10, 10.5, 11, 11.5, 12],
        }, self.dates)
        curve, code, basis, note = _resolve_metrics_benchmark(
            sctx, _Cfg(), "sh000300", self.dates)
        self.assertEqual((code, basis, note), ("sh000300", "price", "tr_late_start"))
        self.assertEqual(curve[0]["date"], self.dates[0])

    def test_explicit_override_respected_even_when_equal_to_price_bench(self):
        sctx = _sctx({"sh000300": [10, 11, 12, 13, 14]}, self.dates)
        curve, code, basis, note = _resolve_metrics_benchmark(
            sctx, _Cfg(metrics_benchmark_code="sh000300"), "sh000300", self.dates)
        self.assertEqual((code, basis), ("sh000300", "price"))
        self.assertIsNone(note)

    def test_explicit_custom_code_missing_yields_empty_curve_with_note(self):
        sctx = _sctx({}, self.dates)
        curve, code, basis, note = _resolve_metrics_benchmark(
            sctx, _Cfg(metrics_benchmark_code="my_index"), "sh000300", self.dates)
        self.assertEqual(curve, [])
        self.assertEqual((code, basis, note), ("my_index", "custom", "explicit_missing"))


if __name__ == "__main__":
    unittest.main()
