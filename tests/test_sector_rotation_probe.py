"""行业轮动探测纯函数单测(情绪/轮动策略/阶梯减仓)。

探测的回测集成靠 data/stockfu.db 真实申万指数数据验证,此处只验纯函数边界与策略语义。
"""
from __future__ import annotations

import unittest


class TestComputeSentiment(unittest.TestCase):
    def _panic(self):
        """120 平盘 + 末 5 日 -3%/日(平量)→ 高恐慌。"""
        closes = [100.0] * 120
        for _ in range(5):
            closes.append(closes[-1] * 0.97)
        amounts = [1e8] * len(closes)
        return closes, amounts

    def _calm(self):
        """缓慢平稳上行 + 平量 → 低恐慌。"""
        closes = [100.0 + i * 0.03 for i in range(125)]
        amounts = [1e8] * len(closes)
        return closes, amounts

    def test_panic_vs_calm(self):
        """恐慌片 段 fear 显著高于 平静片段,且恐慌片 段 fear>60。"""
        from stockfu.backtest.probes.sector_rotation import compute_sentiment
        p = compute_sentiment(*self._panic())
        c = compute_sentiment(*self._calm())
        self.assertIsNotNone(p)
        self.assertIsNotNone(c)
        self.assertGreater(p["fear"], 60.0)
        self.assertGreater(p["fear"], c["fear"])      # 恐慌 > 平静

    def test_panic_low_greed(self):
        """下跌(平量)→ 跌幅分位低 → greed 低(<45)。"""
        from stockfu.backtest.probes.sector_rotation import compute_sentiment
        p = compute_sentiment(*self._panic())
        self.assertIsNotNone(p)
        self.assertLess(p["greed"], 45.0)

    def test_volume_spike_heat(self):
        """放量(价平)→ 成交额分位高 → heat 高(>70)。注:同一 amount 同时进 greed,故 greed 也升。"""
        from stockfu.backtest.probes.sector_rotation import compute_sentiment
        closes = [100.0] * 125
        amounts = [1e8] * 120 + [5e8] * 5             # 末 5 日 5× 放量
        s = compute_sentiment(closes, amounts)
        self.assertIsNotNone(s)
        self.assertGreater(s["heat"], 70.0)

    def test_short_series_none(self):
        """样本 <30 → None(该行业当日排除)。"""
        from stockfu.backtest.probes.sector_rotation import compute_sentiment
        self.assertIsNone(compute_sentiment([100.0, 99.0, 98.0], [1e8, 1e8, 1e8]))
        self.assertIsNone(compute_sentiment([], []))


class TestRotationPolicy(unittest.TestCase):
    def _cross(self):
        """10+ 假行业横截面:3 fear-top + 3 greed-top + 3 heat-top(互不重叠)+ 5 合格 + 1 pct_b 超阈 + 1 低恐。"""
        return {
            # 各指标 top3 → 并集排除
            "F1": {"fear": 95, "greed": 10, "heat": 10, "pct_b": 0.10},
            "F2": {"fear": 92, "greed": 10, "heat": 10, "pct_b": 0.10},
            "F3": {"fear": 90, "greed": 10, "heat": 10, "pct_b": 0.10},
            "G1": {"fear": 50, "greed": 95, "heat": 10, "pct_b": 0.10},
            "G2": {"fear": 50, "greed": 92, "heat": 10, "pct_b": 0.10},
            "G3": {"fear": 50, "greed": 90, "heat": 10, "pct_b": 0.10},
            "H1": {"fear": 50, "greed": 10, "heat": 95, "pct_b": 0.10},
            "H2": {"fear": 50, "greed": 10, "heat": 92, "pct_b": 0.10},
            "H3": {"fear": 50, "greed": 10, "heat": 90, "pct_b": 0.10},
            # 5 个合格(fear 60-70 / greed<40 / pct_b≤0.3),selection_score 递减
            "P": {"fear": 70, "greed": 20, "heat": 20, "pct_b": 0.05},
            "Q": {"fear": 65, "greed": 25, "heat": 20, "pct_b": 0.10},
            "R": {"fear": 62, "greed": 30, "heat": 20, "pct_b": 0.15},
            "S": {"fear": 60, "greed": 35, "heat": 20, "pct_b": 0.20},
            "T": {"fear": 60, "greed": 38, "heat": 20, "pct_b": 0.28},
            # pct_b 超阈被剔
            "U": {"fear": 70, "greed": 20, "heat": 20, "pct_b": 0.50},
            # 低恐候选(low 模式用)
            "L": {"fear": 30, "greed": 20, "heat": 20, "pct_b": 0.10},
        }

    def test_exclude_union(self):
        """fear/greed/heat 各 top3 全部被排除(并集),合格的不含它们。"""
        from stockfu.backtest.probes.sector_rotation import rotation_policy
        t = rotation_policy(self._cross(), panic_direction="high", max_positions=8)
        for excl in ("F1", "F2", "F3", "G1", "G2", "G3", "H1", "H2", "H3", "U"):
            self.assertNotIn(excl, t, f"{excl} 应被排除/剔除")

    def test_rank_and_cap(self):
        """max_positions=3 → 只持 selection_score 最高的 P/Q/R(排序+持仓上限,非填满)。"""
        from stockfu.backtest.probes.sector_rotation import rotation_policy
        t = rotation_policy(self._cross(), panic_direction="high", max_positions=3)
        self.assertEqual(set(t.keys()), {"P", "Q", "R"})

    def test_no_force_fill(self):
        """max_positions=10 但只有 5 个合格 → 只持 5 个,不凑数填满。"""
        from stockfu.backtest.probes.sector_rotation import rotation_policy
        t = rotation_policy(self._cross(), panic_direction="high", max_positions=10)
        self.assertEqual(set(t.keys()), {"P", "Q", "R", "S", "T"})

    def test_sizing_by_closeness(self):
        """越贴下轨(pct_b 越低)→ 定仓越大:weight(P) > weight(R)。"""
        from stockfu.backtest.probes.sector_rotation import rotation_policy
        t = rotation_policy(self._cross(), panic_direction="high", max_positions=8)
        self.assertGreater(t["P"], t["R"])

    def test_low_direction(self):
        """panic_direction=low → 选低恐(fear≤40):L 入选,高恐的 P 不入选。"""
        from stockfu.backtest.probes.sector_rotation import rotation_policy
        t = rotation_policy(self._cross(), panic_direction="low", max_positions=8)
        self.assertIn("L", t)
        self.assertNotIn("P", t)

    def test_no_fear_exclude_keeps_fear_tops(self):
        """no_fear_exclude:fear top(F1-3)可入选;greed/heat top 仍排除。"""
        from stockfu.backtest.probes.sector_rotation import rotation_policy
        t = rotation_policy(self._cross(), panic_direction="high", max_positions=10,
                            sentiment_mode="no_fear_exclude")
        self.assertIn("F1", t)          # 高恐+低贪+贴下轨,不再被 fear 排除
        for excl in ("G1", "G2", "G3", "H1", "H2", "H3"):
            self.assertNotIn(excl, t)

    def test_price_only_ignores_sentiment_gates(self):
        """price_only:不看 fear/greed 门槛;U(pct_b=0.5)仍因 %b 超阈剔除;F1 可贴下轨入选。"""
        from stockfu.backtest.probes.sector_rotation import rotation_policy
        t = rotation_policy(self._cross(), panic_direction="high", max_positions=10,
                            boll_buy_max=0.3, sentiment_mode="price_only")
        self.assertNotIn("U", t)
        # G1 greed 极高但 price_only 不筛 greed,pct_b=0.10 → 可入选
        self.assertIn("G1", t)
        self.assertIn("F1", t)


class TestRegimeScale(unittest.TestCase):
    def test_ma_risk_off(self):
        from stockfu.backtest.probes.sector_rotation import regime_scale
        # 60 根 100 + 跌到 90 → 低于 MA → 0
        closes = [100.0] * 60 + [90.0]
        self.assertEqual(regime_scale(closes, regime="ma", ma_window=60), 0.0)
        self.assertEqual(regime_scale(closes, regime="off"), 1.0)
        # 上行在均线上方
        up = [100.0 + i * 0.1 for i in range(61)]
        self.assertEqual(regime_scale(up, regime="ma", ma_window=60), 1.0)

    def test_dd_risk_off(self):
        from stockfu.backtest.probes.sector_rotation import regime_scale, _apply_regime
        closes = [100.0, 110.0, 100.0, 90.0]  # peak 110 → dd≈18%
        self.assertEqual(regime_scale(closes, regime="dd", dd_limit=0.15), 0.0)
        self.assertEqual(regime_scale(closes, regime="dd", dd_limit=0.25), 1.0)
        self.assertEqual(_apply_regime({"A": 0.2, "B": 0.0}, 0.0), {"A": 0.0, "B": 0.0})
        self.assertEqual(_apply_regime({"A": 0.2}, 0.5)["A"], 0.1)


class TestLadderWeight(unittest.TestCase):
    def test_ladder(self):
        """接近上轨阶梯减仓:0.75 不减 / 0.85 留半 / 1.0 清 / 0.50 不动。"""
        from stockfu.backtest.probes.sector_rotation import _ladder_weight
        w = 0.10
        self.assertAlmostEqual(_ladder_weight(0.50, w), w)       # 未触发
        self.assertAlmostEqual(_ladder_weight(0.75, w), w)       # ≥0.70 仍留全
        self.assertAlmostEqual(_ladder_weight(0.85, w), w * 0.5) # 留半
        self.assertAlmostEqual(_ladder_weight(1.00, w), 0.0)     # 清仓


if __name__ == "__main__":
    unittest.main()
