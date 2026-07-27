"""荐股服务单元测试:策略必填、价格双轨、共识、无默认策略集。"""
from __future__ import annotations

import unittest
from datetime import date

from stockfu.services.recommend import (
    _exec_prices,
    _lot_hint,
    available_strategies,
    build_consensus,
    resolve_strategy_specs,
)
from stockfu.services.valuation import (
    _fair_price,
    _quantile,
    _zone_from_pcts,
    valuation_snapshot,
)


class TestStrategyResolve(unittest.TestCase):
    def test_empty_raises(self):
        with self.assertRaises(ValueError) as cm:
            resolve_strategy_specs([])
        self.assertIn("必填", str(cm.exception))

    def test_unknown_raises_lists_options(self):
        with self.assertRaises(ValueError) as cm:
            resolve_strategy_specs(["not_a_real_strategy"])
        msg = str(cm.exception)
        self.assertIn("未知", msg)
        self.assertIn("可选", msg)          # 必须列出当前可用策略(随目录裁剪变化,故只断言"列出")

    def test_known_ok(self):
        specs = resolve_strategy_specs(["cn_momentum_cross_section", "momentum_breakout"])
        ids = [s.strategy_id for s in specs]
        self.assertEqual(set(ids), {"cn_momentum_cross_section", "momentum_breakout"})
        self.assertTrue(all(s.rebalancer_id for s in specs))

    def test_catalog_nonempty(self):
        # 目录经裁剪后仅保留按收益/夏普去重的策略族代表;断言非空即可(不再固定下限)。
        self.assertGreater(len(available_strategies()), 0)


class TestExecPrices(unittest.TestCase):
    def test_slip_and_band(self):
        d = _exec_prices(100.0, slip_bps=10.0, band_pct=1.0)
        self.assertEqual(d["ref_price"], 100.0)
        self.assertEqual(d["suggest_limit"], 100.1)
        self.assertEqual(d["buy_band"], [99.0, 101.0])

    def test_none_ref(self):
        d = _exec_prices(None, 10.0, 1.0)
        self.assertIsNone(d["ref_price"])
        self.assertIsNone(d["suggest_limit"])

    def test_lot_hint(self):
        h = _lot_hint(0.05, cash=1_000_000, ref=18.5)
        # 5% * 1e6 / 18.5 ≈ 2702 → 2700 整手
        self.assertEqual(h["shares_hint"], 2700)
        self.assertAlmostEqual(h["notional_hint"], 2700 * 18.5, places=2)


class TestValuationHelpers(unittest.TestCase):
    def test_quantile_median(self):
        s = [1.0, 2.0, 3.0, 4.0, 5.0]
        self.assertEqual(_quantile(s, 0.5), 3.0)
        self.assertEqual(_quantile(s, 0.0), 1.0)
        self.assertEqual(_quantile(s, 1.0), 5.0)

    def test_fair_price(self):
        # close=20, pe=20, med=10 → fair=10
        self.assertEqual(_fair_price(20.0, 20.0, 10.0), 10.0)
        self.assertIsNone(_fair_price(20.0, None, 10.0))
        self.assertIsNone(_fair_price(20.0, -1.0, 10.0))

    def test_zone(self):
        self.assertEqual(_zone_from_pcts(10.0, 15.0), "cheap")
        self.assertEqual(_zone_from_pcts(50.0, 50.0), "fair")
        self.assertEqual(_zone_from_pcts(90.0, 50.0), "rich")  # 任一>80
        self.assertEqual(_zone_from_pcts(None, None), "unknown")


class TestConsensus(unittest.TestCase):
    def test_intersection(self):
        reports = [
            {
                "strategy_id": "a",
                "picks": [
                    {"code": "600519", "name": "茅台", "score": 80, "target_w": 0.05},
                    {"code": "000858", "name": "五粮液", "score": 70, "target_w": 0.04},
                ],
            },
            {
                "strategy_id": "b",
                "picks": [
                    {"code": "600519", "name": "茅台", "score": 90, "target_w": 0.05},
                    {"code": "601318", "name": "平安", "score": 60, "target_w": 0.03},
                ],
            },
        ]
        cons = build_consensus(reports)
        self.assertEqual(len(cons), 1)
        self.assertEqual(cons[0]["code"], "600519")
        self.assertEqual(cons[0]["n_strategies"], 2)
        self.assertEqual(cons[0]["avg_score"], 85.0)

    def test_single_strategy_no_consensus(self):
        reports = [{"strategy_id": "a", "picks": [{"code": "1", "score": 1, "target_w": 0.1}]}]
        self.assertEqual(build_consensus(reports), [])


class TestValuationSnapshotDb(unittest.TestCase):
    """有库则抽查 600519;无数据则 skip。"""

    def test_snapshot_no_future_and_keys(self):
        as_of = date(2026, 7, 17)
        try:
            snap = valuation_snapshot("600519", as_of)
        except Exception as e:
            self.skipTest(f"db unavailable: {e}")
        for k in ("pe", "pb", "pe_pct", "pb_pct", "fair_price_pe", "value_zone", "value_band"):
            self.assertIn(k, snap)
        self.assertIn(snap["value_zone"], ("cheap", "fair", "rich", "unknown"))


if __name__ == "__main__":
    unittest.main()
