"""涨跌停 / 滑点 / 停牌 表驱动单测。"""
from __future__ import annotations

import unittest

from stockfu.services.tradeability import (
    ExecutionRules, apply_slip, check_fill, infer_pre_close, is_limit_locked,
)


class TestLimitLocked(unittest.TestCase):
    def test_open_at_limit_up_blocks_buy(self):
        # pre=10, open=11 → +10% 主板涨停
        why = is_limit_locked(
            "buy", pct_chg=10.0, open_=11.0, high=11.0, low=11.0, close=11.0,
            board="main", pre_close=10.0,
        )
        self.assertEqual(why, "limit_up_no_buy")

    def test_open_at_limit_down_blocks_sell(self):
        why = is_limit_locked(
            "sell", pct_chg=-10.0, open_=9.0, high=9.0, low=9.0, close=9.0,
            board="main", pre_close=10.0,
        )
        self.assertEqual(why, "limit_down_no_sell")

    def test_open_not_limit_allows(self):
        why = is_limit_locked(
            "buy", pct_chg=5.0, open_=10.5, high=10.8, low=10.2, close=10.6,
            board="main", pre_close=10.0,
        )
        self.assertIsNone(why)

    def test_st_5pct_board(self):
        # ST +5% 开盘顶格
        why = is_limit_locked(
            "buy", pct_chg=5.0, open_=10.5, high=10.5, low=10.5, close=10.5,
            board="main", is_st=True, pre_close=10.0,
        )
        self.assertEqual(why, "limit_up_no_buy")

    def test_chinext_20pct(self):
        why = is_limit_locked(
            "buy", pct_chg=20.0, open_=12.0, high=12.0, low=12.0, close=12.0,
            board="chinext", pre_close=10.0,
        )
        self.assertEqual(why, "limit_up_no_buy")
        why2 = is_limit_locked(
            "buy", pct_chg=15.0, open_=11.5, high=11.8, low=11.0, close=11.6,
            board="chinext", pre_close=10.0,
        )
        self.assertIsNone(why2)


class TestCheckFill(unittest.TestCase):
    def test_suspended(self):
        r = check_fill("buy", 10.0, trade_status=0)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "suspended")
        self.assertEqual(r.status, "deferred")

    def test_slip_buy(self):
        r = check_fill("buy", 10.0, pct_chg=1.0, open_=10.0, high=10.2,
                       low=9.9, close=10.1, pre_close=9.9,
                       rules=ExecutionRules(slip_bps=10))
        self.assertTrue(r.ok)
        self.assertAlmostEqual(r.price, 10.0 * 1.001, places=6)

    def test_infer_pre_close(self):
        pre = infer_pre_close(11.0, 10.0)
        self.assertAlmostEqual(pre, 10.0, places=6)


class TestSlip(unittest.TestCase):
    def test_buy_sell(self):
        self.assertAlmostEqual(apply_slip(100, "buy", 10), 100.1, places=6)
        self.assertAlmostEqual(apply_slip(100, "sell", 10), 99.9, places=6)
        self.assertEqual(apply_slip(100, "buy", 0), 100)


if __name__ == "__main__":
    unittest.main()
