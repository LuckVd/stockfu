"""Rebalancer 换手抑制单元测试(偏离阈值 + 冷却 + 最小持仓 + 退化)。"""
from __future__ import annotations

import unittest
from datetime import date, timedelta
from types import SimpleNamespace

from stockfu.strategy.rebalancer import Rebalancer

D0 = date(2024, 1, 2)


def policy(drift=0.0, cd=0, mhd=0, sl=None):
    return SimpleNamespace(rebalance_drift=drift, cooldown_days=cd,
                           min_holding_days=mhd, stop_loss_pct=sl)


class TestRebalancer(unittest.TestCase):
    def test_degenerate_all_off(self):
        # 全关 → 有偏离的进、调出的清仓;无偏离的不进(等价旧"全量"的执行结果)
        r = Rebalancer(policy())
        ideal = {"A": 0.10, "B": 0.20}
        cur = {"A": 0.05, "B": 0.20, "C": 0.30}      # B 无偏离,C 持有但不在 ideal
        out = r.decide(ideal, cur, {"A", "B", "C"}, D0)
        self.assertEqual(out, {"A": 0.10, "C": 0.0})

    def test_drift_suppress_and_trigger(self):
        r = Rebalancer(policy(drift=0.03))
        cur = {"A": 0.20}
        held = {"A"}
        self.assertEqual(r.decide({"A": 0.22}, cur, held, D0), {})       # 偏离 0.02 < 0.03 不调
        self.assertEqual(r.decide({"A": 0.24}, cur, held, D0), {"A": 0.24})  # 偏离 0.04 > 0.03 调

    def test_cooldown_blocks_add(self):
        r = Rebalancer(policy(drift=0.0, cd=10))
        r.record_buy("A", D0, was_new=True)
        cur = {"A": 0.10}
        held = {"A"}
        self.assertEqual(r.decide({"A": 0.20}, cur, held, D0 + timedelta(days=5)), {})   # 冷却内
        self.assertEqual(r.decide({"A": 0.20}, cur, held, D0 + timedelta(days=11)), {"A": 0.20})  # 冷却结束

    def test_min_holding_blocks_reduce(self):
        r = Rebalancer(policy(drift=0.0, mhd=10))
        r.record_buy("A", D0, was_new=True)
        cur = {"A": 0.20}
        held = {"A"}
        self.assertEqual(r.decide({"A": 0.10}, cur, held, D0 + timedelta(days=5)), {})   # 最小持仓内减仓被挡
        self.assertEqual(r.decide({"A": 0.10}, cur, held, D0 + timedelta(days=11)), {"A": 0.10})  # 期满放行

    def test_new_position_not_blocked(self):
        # 新建仓(不在 held)不受冷却/最小持仓约束
        r = Rebalancer(policy(cd=100, mhd=100))
        out = r.decide({"A": 0.10}, {"A": 0.0}, set(), D0)
        self.assertEqual(out, {"A": 0.10})

    def test_min_holding_blocks_clear(self):
        r = Rebalancer(policy(mhd=10))
        r.record_buy("A", D0, was_new=True)
        held = {"A"}
        self.assertEqual(r.decide({}, {"A": 0.20}, held, D0 + timedelta(days=5)), {})    # 未满不清
        self.assertEqual(r.decide({}, {"A": 0.20}, held, D0 + timedelta(days=11)), {"A": 0.0})  # 满了清

    def test_record_close_clears_state(self):
        r = Rebalancer(policy(mhd=10))
        r.record_buy("A", D0, was_new=True)
        r.record_close("A")
        self.assertNotIn("A", r.holding_since)
        # 状态清空后,再次清仓不再受 min_holding 阻挡
        self.assertEqual(r.decide({}, {"A": 0.20}, {"A"}, D0 + timedelta(days=1)), {"A": 0.0})

    def test_record_buy_add_does_not_reset_holding_since(self):
        # 加仓(was_new=False)只刷新 last_buy_date,不改 holding_since
        r = Rebalancer(policy(cd=5))
        r.record_buy("A", D0, was_new=True)
        r.record_buy("A", D0 + timedelta(days=2), was_new=False)
        self.assertEqual(r.holding_since["A"], D0)
        self.assertEqual(r.last_buy_date["A"], D0 + timedelta(days=2))

    def test_stop_loss_exemption(self):
        # min_holding 锁定期内,浮亏 ≥ stop_loss_pct → 豁免允许卖(软锁:该止损能止损)
        r = Rebalancer(policy(mhd=60, sl=0.10))
        r.record_buy("A", D0, was_new=True)
        held = {"A"}
        # 锁定期内 + 浮亏 -15%(< -10%)→ 豁免,清仓放行
        self.assertEqual(r.decide({}, {"A": 0.20}, held, D0 + timedelta(days=5), {"A": -0.15}), {"A": 0.0})
        # 锁定期内 + 浮亏 -5%(> -10%)→ 不豁免,锁住
        self.assertEqual(r.decide({}, {"A": 0.20}, held, D0 + timedelta(days=5), {"A": -0.05}), {})
        # 锁定期满 → 不论浮亏都放行
        self.assertEqual(r.decide({}, {"A": 0.20}, held, D0 + timedelta(days=61), {"A": 0.50}), {"A": 0.0})

    def test_risk_exit_bypasses_min_holding_lock(self):
        # V2 risk 的强制止损/止盈不能被 portfolio 的最小持仓锁吞掉。
        r = Rebalancer(policy(mhd=60))
        r.record_buy("A", D0, was_new=True)
        self.assertEqual(
            r.decide({}, {"A": 0.20}, {"A"}, D0 + timedelta(days=5),
                       risk_exit_codes={"A"}),
            {"A": 0.0},
        )

    def test_rank_protection_blocks_reduce_and_clear(self):
        # 前 20% 保护同时覆盖“降权”和“跌出 top15 后清仓”。
        r = Rebalancer(policy())
        held = {"A"}
        self.assertEqual(
            r.decide({"A": 0.10}, {"A": 0.20}, held, D0,
                     protected_codes={"A"}),
            {},
        )
        self.assertEqual(
            r.decide({}, {"A": 0.20}, held, D0,
                     protected_codes={"A"}),
            {},
        )

    def test_rank_protection_does_not_block_risk_exit(self):
        # 排名保护不是风险覆盖，止损/止盈仍可卖出。
        r = Rebalancer(policy())
        self.assertEqual(
            r.decide({}, {"A": 0.20}, {"A"}, D0,
                     risk_exit_codes={"A"}, protected_codes={"A"}),
            {"A": 0.0},
        )

    def test_soft_lock_then_rank_protection(self):
        # 建仓后 30 个交易日内即使跌出前 20%也不普通卖；期满后跌出才放行。
        r = Rebalancer(policy(mhd=30))
        r.record_buy("A", D0, was_new=True, trading_day_index=0)
        self.assertEqual(
            r.decide({}, {"A": 0.20}, {"A"}, D0 + timedelta(days=60),
                     trading_day_index=10),
            {},
        )
        self.assertEqual(
            r.decide({}, {"A": 0.20}, {"A"}, D0 + timedelta(days=60),
                     protected_codes={"A"}, trading_day_index=29),
            {},
        )
        self.assertEqual(
            r.decide({}, {"A": 0.20}, {"A"}, D0 + timedelta(days=60),
                     trading_day_index=30),
            {"A": 0.0},
        )


if __name__ == "__main__":
    unittest.main()
