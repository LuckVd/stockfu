"""_metrics 水下分布 + 回本指标单测(纯函数,无 DB)。

验新增 max_drawdown_recovery_days / max_drawdown_recovered / underwater_pct_*,
并断言旧 max_drawdown / total_return 不变(新增循环仅追踪 index,不改 max_dd 算法 → 回归)。
"""
from __future__ import annotations

import unittest


def _eq(values: list[float], initial: float = 100.0, days: int | None = None):
    from stockfu.backtest.engine import _metrics
    eq_curve = [{"equity": v} for v in values]
    return _metrics(eq_curve, [], initial, days if days is not None else len(values))


class TestMetricsRecovery(unittest.TestCase):
    def test_recovery_after_full_rebound(self):
        # 100→90→80→100:回撤 20%,谷底(idx2)→ 收回前高(idx3)= 1 个交易日。
        m = _eq([100, 90, 80, 100])
        self.assertEqual(m["max_drawdown"], 20.0)
        self.assertTrue(m["max_drawdown_recovered"])
        self.assertEqual(m["max_drawdown_recovery_days"], 1)
        # 水下:idx1(10%)、idx2(20%)水下;idx0/idx3 平水。手算核对(分母 n=4):
        self.assertEqual(m["underwater_pct_gt0"], 50.0)   # 2/4
        self.assertEqual(m["underwater_pct_ge10"], 50.0)  # 2/4
        self.assertEqual(m["underwater_pct_ge20"], 25.0)  # 1/4
        self.assertEqual(m["underwater_pct_ge30"], 0.0)   # 0/4

    def test_no_recovery_still_underwater(self):
        # 100→80→90:回撤 20%,期末 90 未收回前高 100 → 未回本。
        m = _eq([100, 80, 90])
        self.assertEqual(m["max_drawdown"], 20.0)
        self.assertFalse(m["max_drawdown_recovered"])
        self.assertIsNone(m["max_drawdown_recovery_days"])
        # idx1(20%)、idx2(10%)水下;idx0 平水(分母 n=3):
        self.assertEqual(m["underwater_pct_gt0"], round(2 / 3 * 100, 1))   # 66.7
        self.assertEqual(m["underwater_pct_ge20"], round(1 / 3 * 100, 1))  # 33.3
        self.assertEqual(m["underwater_pct_ge30"], 0.0)

    def test_deep_drawdown_ge30(self):
        # 100→60:回撤 40%,1/2 点 ≥30% 水下。
        m = _eq([100, 60])
        self.assertEqual(m["max_drawdown"], 40.0)
        self.assertFalse(m["max_drawdown_recovered"])
        self.assertIsNone(m["max_drawdown_recovery_days"])
        self.assertEqual(m["underwater_pct_ge30"], 50.0)

    def test_regression_old_keys_unchanged(self):
        # 新增水下/回本循环不改 max_dd 算法 → 旧指标值不变。
        m = _eq([100, 110, 105, 120])
        self.assertEqual(m["total_return"], 20.0)
        # 峰值 110(idx1)→ 谷 105(idx2):回撤 (110-105)/110 = 4.55%
        self.assertEqual(m["max_drawdown"], round((110 - 105) / 110 * 100, 2))
        self.assertIn("annualized", m)
        # 这条曲线也回本了(120 > 前高 110)
        self.assertTrue(m["max_drawdown_recovered"])


if __name__ == "__main__":
    unittest.main()
