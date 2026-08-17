"""V2SignalScorer 的 raw 注册键：metric|指纹（2026-08-17 审查修复的回归）。

旧实现按 metric 单键注册，「先注册者胜」：TEN_RESEARCH_ALPHAS 中
reversal_20d_v2（momentum window=20）与 momentum_252d_skip21_v2（window=252）
共用 metric "momentum"，反转因子被静默算成 12 个月动量且无报错。
"""
from __future__ import annotations

import unittest

from stockfu.services.universe import UniverseRules
from stockfu.services.v2_signal import V2SignalScorer


def _scorer(alpha_ids: list[str]) -> V2SignalScorer:
    return V2SignalScorer(
        alpha_ids,
        universe_rules=UniverseRules(),
        codes=["600519"],
    )


class TestRawKeyIsolation(unittest.TestCase):
    def test_same_metric_different_params_get_distinct_keys(self):
        s = _scorer(["momentum_jt_v2", "reversal_jl_v2"])
        mom_key = s.pid_to_raw_key["momentum_252d_skip21_v2"]
        rev_key = s.pid_to_raw_key["reversal_20d_v2"]
        self.assertNotEqual(mom_key, rev_key)
        # 各自参数独立、与 profile 声明一致
        self.assertEqual(s.raw_params[mom_key]["window"], 252)
        self.assertEqual(s.raw_params[rev_key]["window"], 20)
        self.assertEqual(s.raw_key_metric[mom_key], "momentum")
        self.assertEqual(s.raw_key_metric[rev_key], "momentum")
        # 指纹也各自独立
        self.assertNotEqual(s.raw_fingerprints[mom_key], s.raw_fingerprints[rev_key])

    def test_same_metric_same_params_share_one_key(self):
        s = _scorer(["momentum_jt_v2", "multi_factor_v2"])
        k1 = s.pid_to_raw_key["momentum_252d_skip21_v2"]
        # multi_factor_v2 也引用 momentum_252d_skip21_v2 → 同键复用，不重复计算
        k2 = s.pid_to_raw_key["momentum_252d_skip21_v2"]
        self.assertEqual(k1, k2)
        momentum_keys = [k for k, m in s.raw_key_metric.items() if m == "momentum"]
        self.assertEqual(len(momentum_keys), 1)


if __name__ == "__main__":
    unittest.main()
