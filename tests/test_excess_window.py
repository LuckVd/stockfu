"""excess 交集窗口语义回归(2026-08-24 审查修复 M1)。

此前 excess = total_return(全窗口) - benchmark_return(基准自身窗口):
基准首点晚于回测起点时分子分母窗口错位。修复后策略收益从基准窗口首日
当日 equity 起算,与基准同窗对比;基准覆盖全窗口时与旧行为一致。
"""
from __future__ import annotations

import unittest
from datetime import date

from stockfu.backtest.engine import _metrics


def _curve(equities: list[float], start: date):
    d = start
    days = []
    cur = []
    for e in equities:
        days.append(d.isoformat())
        cur.append({"date": d.isoformat(), "equity": e})
        d = date.fromordinal(d.toordinal() + 3)   # 跳过周末近似
    return cur


class TestExcessIntersectionWindow(unittest.TestCase):
    def setUp(self):
        self.start = date(2024, 1, 2)

    def test_benchmark_covers_full_window_matches_old_behavior(self):
        eq = _curve([100.0, 102.0, 104.0, 106.0], self.start)
        bm = _curve([100.0, 101.0, 102.0, 103.0], self.start)
        m = _metrics(eq, bm, 100.0, 4)
        self.assertEqual(m["total_return"], 6.0)
        self.assertEqual(m["benchmark_return"], 3.0)
        self.assertEqual(m["excess"], 3.0)

    def test_late_benchmark_start_uses_intersection_window(self):
        """基准首点晚于回测起点:excess 按交集窗口,不再错位相减。"""
        eq = _curve([100.0, 110.0, 120.0, 132.0], self.start)     # 全窗 +32%
        # 基准从第 2 天才有数据(窗口起点 = 第 2 天)
        bm_dates = [p["date"] for p in eq][1:]
        bm = [{"date": d, "equity": e} for d, e in zip(
            bm_dates, [100.0, 110.0, 121.0])]                     # 交集窗 +21%
        window = {"start": bm_dates[0], "end": bm_dates[-1]}
        m = _metrics(eq, bm, 100.0, 4, bench_window=window)
        self.assertEqual(m["benchmark_return"], 21.0)
        # 策略收益从基准窗口首日(equity=110)起算: 132/110-1 = 20%
        self.assertEqual(m["excess"], round(20.0 - 21.0, 2))      # -1.0

    def test_no_benchmark_yields_none(self):
        eq = _curve([100.0, 102.0], self.start)
        m = _metrics(eq, [], 100.0, 2)
        self.assertIsNone(m["benchmark_return"])
        self.assertIsNone(m["excess"])


if __name__ == "__main__":
    unittest.main()
