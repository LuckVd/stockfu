"""情绪三指标的核心口径：方向独立、热度对规模变化不失真。"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from stockfu.services import composite as C
from stockfu.services import factors as F


class TestCompositeSentiment(unittest.TestCase):
    def test_market_benchmark_matches_displayed_shanghai_index(self):
        self.assertEqual(C.BENCH, "sh000001")

    def test_relative_activity_detects_spike_without_absolute_scale_bias(self):
        values = [100.0] * 120 + [500.0] * 5
        activity = C._rolling_relative_activity(values)
        pct = F.percentile(activity, activity[-1])[0]
        self.assertIsNotNone(pct)
        self.assertGreater(pct, 70.0)

    def test_volume_spike_enters_heat_not_greed(self):
        closes = [100.0] * 125
        values = [100.0] * 120 + [500.0] * 5
        with patch.object(F, "quote_series", side_effect=[closes, values, values]):
            result = C.compute_for("demo", "stock", "demo")

        self.assertEqual(result["greed"], 50.0)  # 平价的动量分位，不受放量影响
        self.assertGreater(result["heat"], 70.0)
        self.assertIn("relative_activity_pct", result["components"])
        self.assertNotIn("amount_pct", result["components"])

    def test_short_amount_series_falls_back_to_volume_for_heat(self):
        closes = [100.0] * 125
        short_amounts = [100.0] * 11
        volumes = [100.0] * 120 + [500.0] * 5
        with patch.object(F, "quote_series", side_effect=[closes, short_amounts, volumes]):
            result = C.compute_for("demo", "stock", "demo")
        self.assertIsNotNone(result["heat"])
        self.assertGreater(result["heat"], 70.0)


if __name__ == "__main__":
    unittest.main()
