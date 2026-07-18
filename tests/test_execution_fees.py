"""执行层费用单测:印花税日期化(P2-3 第一步)。

验 stamp_duty_rate 分段(2023-08-28 前千一 / 之后万五 / None=现行)+ apply_action
卖出在两个日期的 fee 差正好 = 0.0005×proceeds;买方不收印花税,与日期无关。
纯内存 VirtualAccount,不依赖 DB。
"""
from __future__ import annotations

import unittest
from datetime import date


class TestStampDutyRate(unittest.TestCase):
    def test_segments(self):
        from stockfu.backtest.engine import stamp_duty_rate
        self.assertEqual(stamp_duty_rate(date(2021, 1, 4)), 0.001)     # 千一(2023-08-28 前)
        self.assertEqual(stamp_duty_rate(date(2023, 8, 27)), 0.001)    # 生效前一天仍千一
        self.assertEqual(stamp_duty_rate(date(2023, 8, 28)), 0.0005)   # 生效日万五
        self.assertEqual(stamp_duty_rate(date(2025, 6, 2)), 0.0005)    # 之后万五

    def test_none_falls_back_to_latest(self):
        from stockfu.backtest.engine import stamp_duty_rate
        self.assertEqual(stamp_duty_rate(None), 0.0005)   # 无日期=现行最新(实盘即时回退)


class TestApplyActionStampDuty(unittest.TestCase):
    def _sell_fee(self, as_of):
        """建 1000 股@10 → 全卖@10(价平无盈亏),返回手续费。"""
        from stockfu.backtest.engine import VirtualAccount, Position
        acct = VirtualAccount(1_000_000)
        acct.positions["A"] = Position(shares=1000, avg_cost=10.0)
        rec = acct.apply_action("A", "sell", 0.0, 10.0, {"A": 10.0}, as_of=as_of)
        self.assertIsNotNone(rec, "应成交一笔卖出")
        return rec["fee"]

    def test_sell_fee_higher_before_cutoff(self):
        """千一日卖出费 > 万五日,差额正好 = 0.0005×proceeds(=5.0)。"""
        fee_old = self._sell_fee(date(2023, 8, 25))
        fee_new = self._sell_fee(date(2023, 8, 28))
        self.assertAlmostEqual(fee_old - fee_new, 5.0, places=6)
        self.assertGreater(fee_old, fee_new)

    def test_sell_fee_values(self):
        """绝对值:万五日 = 5(min 佣)+10000×(0.0005+0.00001)=10.1;千一日 = 15.1。"""
        self.assertAlmostEqual(self._sell_fee(date(2023, 8, 28)), 10.1, places=2)
        self.assertAlmostEqual(self._sell_fee(date(2023, 8, 25)), 15.1, places=2)

    def test_buy_fee_independent_of_date(self):
        """买方不收印花税:as_of 不影响买入费(两日期 fee 完全相等)。"""
        from stockfu.backtest.engine import VirtualAccount

        def buy_fee(as_of):
            return VirtualAccount(1_000_000).apply_action(
                "A", "buy", 0.10, 10.0, {"A": 10.0}, as_of=as_of)["fee"]

        self.assertEqual(buy_fee(date(2021, 1, 4)), buy_fee(date(2025, 1, 2)))


if __name__ == "__main__":
    unittest.main()
