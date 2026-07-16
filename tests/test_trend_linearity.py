"""linreg_r2(价格对时间线性回归)纯函数单测。

trend_linearity 算子的核心:R²(趋势线性度)+ slope(方向)。算子集成在回测验证,
此处只验纯函数边界与量纲。
"""
from __future__ import annotations

import unittest


class TestLinregR2(unittest.TestCase):
    def test_linear_up(self):
        """严格递增 → r²≈1.0,slope>0(平稳上涨)。"""
        from stockfu.services.factors import linreg_r2
        r2, slope = linreg_r2([float(i) for i in range(1, 21)])
        self.assertAlmostEqual(r2, 1.0, places=5)
        self.assertGreater(slope, 0.0)

    def test_linear_down(self):
        """严格递减 → r²≈1.0,slope<0(平稳下跌:score 应为负)。"""
        from stockfu.services.factors import linreg_r2
        r2, slope = linreg_r2([float(i) for i in range(20, 0, -1)])
        self.assertAlmostEqual(r2, 1.0, places=5)
        self.assertLess(slope, 0.0)

    def test_constant_price(self):
        """价格恒定(方差为 0)→ (0.0, 0.0),不除零。"""
        from stockfu.services.factors import linreg_r2
        r2, slope = linreg_r2([5.0] * 10)
        self.assertEqual((r2, slope), (0.0, 0.0))

    def test_short_series(self):
        """样本<3 → (0.0, 0.0)。"""
        from stockfu.services.factors import linreg_r2
        self.assertEqual(linreg_r2([1.0, 2.0]), (0.0, 0.0))
        self.assertEqual(linreg_r2([]), (0.0, 0.0))

    def test_oscillating_low_r2(self):
        """明显震荡(无趋势)→ r² 低(非平稳,应被 trend_linearity 判 hold)。"""
        from stockfu.services.factors import linreg_r2
        # 上下跳动的序列,线性度差
        series = [10, 12, 9, 13, 8, 14, 7, 15, 6, 16,
                  5, 17, 4, 18, 3, 19, 2, 20, 1, 21]
        r2, _ = linreg_r2(series)
        self.assertLess(r2, 0.5)

    def test_r2_never_exceeds_one(self):
        """浮点误差不得使 r² > 1(算子 score=r²×sign×40 量纲依赖 r²∈[0,1])。"""
        from stockfu.services.factors import linreg_r2
        # 近乎完美线性 + 微噪声,边界检查
        series = [1.0 + 0.1 * i + 1e-9 * ((-1) ** i) for i in range(30)]
        r2, _ = linreg_r2(series)
        self.assertLessEqual(r2, 1.0)
        self.assertGreaterEqual(r2, 0.0)

    def test_score_scale_alignment(self):
        """验证 trend_linearity 的 score 量纲:r²=1/slope>0 → +20,slope<0 → −20。"""
        from stockfu.services.factors import linreg_r2
        r2_up, slope_up = linreg_r2([float(i) for i in range(1, 21)])
        score_up = round(r2_up * (1.0 if slope_up > 0 else -1.0) * 20, 2)
        self.assertAlmostEqual(score_up, 20.0, places=1)
        r2_dn, slope_dn = linreg_r2([float(i) for i in range(20, 0, -1)])
        score_dn = round(r2_dn * (1.0 if slope_dn > 0 else -1.0) * 20, 2)
        self.assertAlmostEqual(score_dn, -20.0, places=1)


if __name__ == "__main__":
    unittest.main()
