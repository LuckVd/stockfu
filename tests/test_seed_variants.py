"""策略变体展开器单测(_expand_variants / _deep_merge,纯函数,无 DB)。

验 base + override 深合并(叶替换、嵌套 dict 兄弟键保留、list 整体替换、新键加入)、
变体 id 形如 base#key、变体 cfg 剥除 variants 键、base 行保留原文。
"""
from __future__ import annotations

import unittest

import yaml

from stockfu.ai.operators.seed import _deep_merge, _expand_variants, _prune_signal_strategy_ids


class TestDeepMerge(unittest.TestCase):
    def test_nested_dict_merged_leaf_replaced_sibling_kept(self):
        base = {"risk": {"stop_loss": 0.08, "portfolio_brake": 0.10}, "name": "x"}
        override = {"risk": {"stop_loss": 0.30}}
        out = _deep_merge(base, override)
        self.assertEqual(out["risk"]["stop_loss"], 0.30)        # 叶被替换
        self.assertEqual(out["risk"]["portfolio_brake"], 0.10)  # 兄弟键保留
        self.assertEqual(out["name"], "x")                      # 非覆盖路径保留
        self.assertEqual(base["risk"]["stop_loss"], 0.08)       # 不改入参

    def test_list_replaced_whole_not_appended(self):
        base = {"operators": [{"id": "a"}, {"id": "b"}]}
        override = {"operators": [{"id": "c"}]}
        out = _deep_merge(base, override)
        self.assertEqual(out["operators"], [{"id": "c"}])  # 整体替换

    def test_scalar_replaced(self):
        out = _deep_merge({"score_full": 8}, {"score_full": 12})
        self.assertEqual(out["score_full"], 12)

    def test_new_nested_key_added(self):
        out = _deep_merge({"name": "x"}, {"risk": {"stop_loss": 0.3}})
        self.assertEqual(out["risk"], {"stop_loss": 0.3})  # base 无 risk → 整体加入


class TestExpandVariants(unittest.TestCase):
    def _base_text(self) -> str:
        return (
            "version: 1\n"
            "name: 红利横截面\n"
            "operators:\n"
            "  - {id: dividend_yield, type: math, weight: 1.0}\n"
            "position:\n"
            "  mode: continuous\n"
            "  score_full: 8\n"
        )

    def test_no_variants_returns_base_only(self):
        rows = _expand_variants("dividend_cross_section", self._base_text())
        self.assertEqual(len(rows), 1)
        sid, name, text, derived = rows[0]
        self.assertEqual(sid, "dividend_cross_section")
        self.assertFalse(derived)
        self.assertIn("红利横截面", name)
        self.assertEqual(text, self._base_text())  # base 行保留原文

    def test_variant_expanded_with_composite_id(self):
        text = self._base_text() + (
            "variants:\n"
            "  - key: sl30\n"
            "    name: 红利横截面(止损30%)\n"
            "    override:\n"
            "      risk:\n"
            "        stop_loss: 0.30\n"
        )
        rows = _expand_variants("dividend_cross_section", text)
        self.assertEqual(len(rows), 2)

        # base 行
        self.assertEqual(rows[0][0], "dividend_cross_section")
        self.assertFalse(rows[0][3])

        # 变体行
        vsid, vname, vtext, derived = rows[1]
        self.assertEqual(vsid, "dividend_cross_section#sl30")
        self.assertTrue(derived)
        self.assertIn("止损30%", vname)

        vcfg = yaml.safe_load(vtext)
        self.assertNotIn("variants", vcfg)                    # variants 键已剥除
        self.assertEqual(vcfg["risk"]["stop_loss"], 0.30)      # override 生效
        self.assertEqual(vcfg["position"]["score_full"], 8)    # base 键保留
        self.assertEqual(vcfg["name"], "红利横截面(止损30%)")


class TestPruneSignalStrategyIds(unittest.TestCase):
    def test_removes_archived_ids_and_deduplicates(self):
        raw = '["keep", "old", "keep", "other"]'
        self.assertEqual(
            _prune_signal_strategy_ids(raw, {"keep", "other"}),
            '["keep", "other"]',
        )

    def test_empty_result_returns_none_for_fallback(self):
        self.assertIsNone(_prune_signal_strategy_ids('["old"]', {"keep"}))

    def test_csv_legacy_value_is_supported(self):
        self.assertEqual(
            _prune_signal_strategy_ids("keep,old", {"keep"}),
            '["keep"]',
        )


if __name__ == "__main__":
    unittest.main()
