"""cash_scaler 表驱动单测:等长契约 + 等比缩放 + 最小手预算。"""
from __future__ import annotations

import unittest
from dataclasses import dataclass, field


@dataclass
class _Pos:
    shares: int = 0
    avg_cost: float = 0.0


@dataclass
class _Acct:
    cash: float
    positions: dict = field(default_factory=dict)
    _prices: dict = field(default_factory=dict)

    def equity(self, prices):
        return self.cash + sum(
            self.positions.get(c, _Pos()).shares * prices.get(c, 0)
            for c in set(prices) | set(self.positions)
        )

    def weight(self, code, prices):
        eq = self.equity(prices)
        if eq <= 0:
            return 0.0
        pos = self.positions.get(code)
        if not pos or pos.shares <= 0:
            return 0.0
        return pos.shares * prices.get(code, 0) / eq


class TestScaleBuysToCash(unittest.TestCase):
    def test_empty(self):
        from stockfu.backtest.cash_scaler import scale_buys_to_cash
        scaled, safety, constrained = scale_buys_to_cash(
            _Acct(1e6), [], {}, commission_rate=0.0003,
            transfer_fee_rate=1e-5, min_commission=5.0)
        self.assertEqual(scaled, [])
        self.assertEqual(safety, 1.0)
        self.assertFalse(constrained)

    def test_length_equals_buys_even_if_some_zero_delta(self):
        """等长契约:即使部分 d<=0,scaled 仍与 buys 等长同序。"""
        from stockfu.backtest.cash_scaler import scale_buys_to_cash
        prices = {"A": 10.0, "B": 20.0, "C": 5.0}
        # 已持 A 权重已达目标 → d≈0;B/C 要买
        acct = _Acct(cash=50_000, positions={"A": _Pos(shares=5000)})  # 5万市值
        # equity ≈ 50k cash + 50k A = 100k; A weight 0.5
        buys = [
            ("A", 0.5, 10.0),   # 已在目标
            ("B", 0.2, 20.0),
            ("C", 0.1, 5.0),
        ]
        scaled, safety, constrained = scale_buys_to_cash(
            acct, buys, prices, commission_rate=0.0003,
            transfer_fee_rate=1e-5, min_commission=5.0)
        self.assertEqual(len(scaled), len(buys))
        self.assertEqual([c for c, _, _ in scaled], ["A", "B", "C"])
        # A 应退回 cur_w ≈ 0.5
        self.assertAlmostEqual(scaled[0][1], acct.weight("A", prices), places=4)

    def test_proportional_scale_by_code(self):
        """现金不足时 safety 等比;按 code 取 scaled_tw 不错位。"""
        from stockfu.backtest.cash_scaler import scale_buys_to_cash
        prices = {"X": 10.0, "Y": 10.0}
        acct = _Acct(cash=10_000)  # 只能支撑一部分
        buys = [
            ("X", 0.5, 10.0),  # 目标 0.5 * equity; equity=cash=10k → 5k
            ("Y", 0.5, 10.0),
        ]
        # 空仓 equity=cash=10000; 两笔各 5000 + fee 远超 10k
        scaled, safety, constrained = scale_buys_to_cash(
            acct, buys, prices, commission_rate=0.0003,
            transfer_fee_rate=1e-5, min_commission=5.0)
        self.assertTrue(constrained)
        self.assertLess(safety, 1.0)
        self.assertEqual(len(scaled), 2)
        by = {c: tw for c, tw, _ in scaled}
        # 两票对称目标 → 缩放后权重接近
        self.assertAlmostEqual(by["X"], by["Y"], places=5)
        self.assertGreater(by["X"], 0)

    def test_min_lot_in_budget(self):
        """空仓碎单目标仍按 1 手计入预算 → 与 apply_action 特例对齐。"""
        from stockfu.backtest.cash_scaler import scale_buys_to_cash
        # 目标增量极小但空仓:scaler 应按 100 股预算
        prices = {"Z": 100.0}  # 1 手 = 10000
        acct = _Acct(cash=5000)  # 不够 1 手
        buys = [("Z", 0.001, 100.0)]  # 极小权重
        scaled, safety, constrained = scale_buys_to_cash(
            acct, buys, prices, commission_rate=0.0003,
            transfer_fee_rate=1e-5, min_commission=5.0)
        self.assertTrue(constrained)
        self.assertEqual(len(scaled), 1)
        # 现金不够 1 手 → safety 压到很低或 0
        self.assertLessEqual(safety, 0.5)


if __name__ == "__main__":
    unittest.main()
