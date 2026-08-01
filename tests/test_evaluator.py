"""通用股票评价引擎(evaluator)单元测试。

重点验证:
  1. 解耦 —— evaluate() 是纯参数函数,不读 watchlist/active;
  2. 共识聚合(多数票/均分/一致性/buy-hold-sell 计数);
  3. 容错(单票 analyze 报错/单策略编译失败/LLM 失败 不阻断);
  4. 股票池装配 --add/--drop 是集合运算且不持久化(不写 DB);
  5. 空池/空策略 → 友好报错不崩。
"""
from __future__ import annotations

import unittest
from contextlib import contextmanager
from datetime import date
from unittest.mock import patch

from sqlmodel import SQLModel, Session, create_engine

from stockfu.services import evaluator
from stockfu.models import Strategy


def _make_strategy_rows() -> list[dict]:
    """两个极简策略 yaml(纯 math,不依赖 LLM/外部数据)。"""
    yaml_a = """
name: 测试策略A
operators:
  - id: dividend_yield
    type: math
    weight: 1.0
aggregate:
  method: weighted_sum
  thresholds: {strong_buy: 5, buy: 1, hold: -1}
position: {max_w: 0.10, dead: 3, score_full: 20}
risk: {stop_loss: 0.30}
""".strip()
    return [
        {"strategy_id": "fake_a", "name": "测试策略A", "config": yaml_a},
    ]


def _scope_factory(engine):
    """构造一个 contextmanager,把 evaluator.session_scope 指到内存库。"""
    @contextmanager
    def _scope():
        with Session(engine) as s:
            yield s
    return _scope


class TestMajoritySignal(unittest.TestCase):
    def test_buy_wins(self):
        self.assertEqual(evaluator._majority_signal(["buy", "buy", "sell"]), "buy")

    def test_sell_wins(self):
        self.assertEqual(evaluator._majority_signal(["sell", "sell", "buy"]), "sell")

    def test_tie_falls_to_hold(self):
        # 平票(各1) → 保守取 hold
        self.assertEqual(evaluator._majority_signal(["buy", "hold", "sell"]), "hold")

    def test_strong_over_weak_same_side(self):
        self.assertEqual(
            evaluator._majority_signal(["strong_buy", "strong_buy", "sell"]), "strong_buy"
        )


class TestConsensus(unittest.TestCase):
    def test_basic_aggregation(self):
        c = evaluator._consensus(
            {"a": {"signal": "buy", "total_score": 10},
             "b": {"signal": "buy", "total_score": 20}},
            ["a", "b"],
        )
        self.assertEqual(c["n_buy"], 2)
        self.assertEqual(c["avg_score"], 15.0)
        self.assertEqual(c["agreement"], 1.0)
        self.assertEqual(c["signal"], "buy")

    def test_error_cell_excluded_from_votes(self):
        c = evaluator._consensus(
            {"a": {"signal": "buy", "total_score": 10},
             "b": {"error": "boom", "signal": "error"}},
            ["a", "b"],
        )
        self.assertEqual(c["n_buy"], 1)
        self.assertEqual(c["n_error"], 1)          # 计入分母(策略总数)
        self.assertEqual(c["agreement"], 1.0)      # 唯一有效票内部一致
        self.assertEqual(c["n_strategies"], 2)

    def test_none_signal_excluded(self):
        c = evaluator._consensus(
            {"a": {"signal": None}, "b": {"signal": "sell", "total_score": -5}},
            ["a", "b"],
        )
        self.assertEqual(c["n_sell"], 1)
        self.assertEqual(c["n_error"], 1)          # None 也算无信号
        self.assertEqual(c["signal"], "sell")


class TestBuildMatrix(unittest.TestCase):
    def test_sorted_by_consensus(self):
        ps = [
            {"strategy_id": "A", "name": "A", "cells": {
                "001": {"signal": "buy", "total_score": 10},
                "002": {"signal": "sell", "total_score": -5}}},
            {"strategy_id": "B", "name": "B", "cells": {
                "001": {"signal": "buy", "total_score": 8},
                "002": {"signal": "hold", "total_score": 0}}},
        ]
        m = evaluator.build_matrix(ps, ["001", "002"])
        self.assertEqual([r["code"] for r in m], ["001", "002"])  # buy 共识排前
        # 001 两个 buy → 共识 buy;002 sell+hold → 平票 hold
        self.assertEqual(m[0]["consensus"]["signal"], "buy")
        self.assertEqual(m[1]["consensus"]["signal"], "hold")

    def test_full_coverage_all_codes_listed(self):
        """全量点评:信号弱的也必须出现(与 recommend.build_consensus 只列交集不同)。"""
        ps = [
            {"strategy_id": "A", "name": "A", "cells": {
                "001": {"signal": "buy", "total_score": 10}}},
        ]
        m = evaluator.build_matrix(ps, ["001", "002", "003"])  # 002/003 无数据
        codes = [r["code"] for r in m]
        self.assertEqual(set(codes), {"001", "002", "003"})     # 全集都在
        # 002/003 的 cell 是默认占位
        self.assertIsNone(m[1]["per_strategy"]["A"]["signal"])

    def test_strategy_compile_error_isolated(self):
        """单策略编译失败 → 该列标 error,不影响其他策略 / 不崩。"""
        ps = [
            {"strategy_id": "BAD", "name": "BAD", "error": "YAMLError: x", "cells": {}},
            {"strategy_id": "A", "name": "A", "cells": {"001": {"signal": "buy", "total_score": 10}}},
        ]
        m = evaluator.build_matrix(ps, ["001"])
        cell_bad = m[0]["per_strategy"]["BAD"]
        self.assertIn("error", cell_bad)
        self.assertEqual(cell_bad["signal"], "error")
        # 好策略列正常
        self.assertEqual(m[0]["per_strategy"]["A"]["signal"], "buy")


class TestReplayBearLatch(unittest.TestCase):
    def test_short_window_no_latch(self):
        self.assertFalse(
            evaluator._replay_bear_latch([1, 2, 3], ma_days=200, enter_band=0, exit_band=0.03)
        )

    def test_monotonic_up_never_latches(self):
        up = [100 + i for i in range(250)]
        self.assertFalse(
            evaluator._replay_bear_latch(up, ma_days=50, enter_band=0, exit_band=0.03)
        )

    def test_crash_latches(self):
        # 先涨到 200 形成高 MA,再跌到 100(破 MA×(1-band)) → 应 latch
        series = [100 + i for i in range(200)] + [100] * 50
        self.assertTrue(
            evaluator._replay_bear_latch(series, ma_days=50, enter_band=0.0, exit_band=0.03)
        )


class TestLoadStrategies(unittest.TestCase):
    def _patch_db(self, rows):
        eng = create_engine("sqlite://")
        SQLModel.metadata.create_all(eng)
        with Session(eng) as s:
            for r in rows:
                s.add(Strategy(**r))
            s.commit()
        scope = _scope_factory(eng)
        return patch.object(evaluator, "session_scope", scope)

    def test_empty_raises(self):
        with self.assertRaises(ValueError) as cm:
            evaluator._load_strategies([])
        self.assertIn("为空", str(cm.exception))

    def test_unknown_raises_lists_options(self):
        rows = [{"strategy_id": "real_one", "name": "x", "config": "name: x"}]
        with self._patch_db(rows):
            with self.assertRaises(ValueError) as cm:
                evaluator._load_strategies(["not_real"])
            msg = str(cm.exception)
            self.assertIn("未知", msg)
            self.assertIn("real_one", msg)  # 列出可选

    def test_dedup_preserves_order(self):
        rows = [
            {"strategy_id": "a", "name": "A", "config": "name: A"},
            {"strategy_id": "b", "name": "B", "config": "name: B"},
        ]
        with self._patch_db(rows):
            loaded = evaluator._load_strategies(["b", "a", "b", "a"])  # 重复
        ids = [x[0] for x in loaded]
        self.assertEqual(ids, ["b", "a"])  # 去重保序


class TestAssembleCodes(unittest.TestCase):
    """装配层:--add/--drop 是集合运算且不持久化(不写 DB)。
    watchlist 走 _true_watchlist_codes(is_watch=True),不走有 bug 的 resolve_base_codes。"""

    def test_add_drop_setops(self):
        # mock _true_watchlist_codes 返回固定自选池
        with patch.object(evaluator, "_true_watchlist_codes", return_value=["001", "002", "003"]):
            # add 004, drop 002
            codes = evaluator.assemble_codes("watchlist", add=["004"], drop=["002"])
        self.assertEqual(set(codes), {"001", "003", "004"})
        self.assertNotIn("002", codes)

    def test_codes_override_bypasses_pool(self):
        # codes_override 直接用显式列表,不碰任何池查询
        with patch.object(evaluator, "_true_watchlist_codes", side_effect=AssertionError("不应被调用")):
            codes = evaluator.assemble_codes("watchlist", codes_override=["111", "222"])
        self.assertEqual(set(codes), {"111", "222"})

    def test_drop_nonexistent_is_noop(self):
        with patch.object(evaluator, "_true_watchlist_codes", return_value=["001"]):
            codes = evaluator.assemble_codes("watchlist", drop=["NOTHERE"])
        self.assertEqual(codes, ["001"])  # drop 不存在的 = no-op

    def test_non_watchlist_pool_uses_resolve_base_codes(self):
        # all / historical_indices 等非 watchlist 池仍走 resolve_base_codes
        with patch("stockfu.services.universe.resolve_base_codes", return_value=["AAA", "BBB"]):
            codes = evaluator.assemble_codes("all")
        self.assertEqual(set(codes), {"AAA", "BBB"})


class TestEvaluateDecoupled(unittest.TestCase):
    """解耦验证:evaluate() 只吃 list 参数,不读 watchlist / active。"""

    def test_evaluate_with_mocked_analyze(self):
        """evaluate() 传任意 codes + strategy_ids 能工作(不碰 watchlist/active)。"""
        rows = [{"strategy_id": "s1", "name": "S1", "config": "name: S1\noperators: []\naggregate: {method: weighted_sum}\n"}]
        eng = create_engine("sqlite://")
        SQLModel.metadata.create_all(eng)
        with Session(eng) as s:
            s.add(Strategy(**rows[0]))
            s.commit()
        scope = _scope_factory(eng)

        # mock _eval_one_strategy 直接返回固定 cells(跳过真实 analyze)
        def fake_eval(sid, yaml_text, codes, as_of, **kw):
            return {c: {"signal": "buy", "total_score": 10.0,
                        "confidence": 0.8, "ai_target_weight": 0.05,
                        "risk_vetoed": False, "factors": {}} for c in codes}

        with patch.object(evaluator, "session_scope", scope), \
             patch.object(evaluator, "_eval_one_strategy", side_effect=fake_eval), \
             patch.object(evaluator, "discover_and_register"):
            report = evaluator.evaluate(["AAA", "BBB"], ["s1"], date(2026, 7, 21))

        self.assertEqual(set(report["codes"]), {"AAA", "BBB"})
        self.assertEqual(report["strategy_ids"], ["s1"])
        self.assertEqual(len(report["matrix"]), 2)
        # 每股每策略一格
        for row in report["matrix"]:
            self.assertIn("s1", row["per_strategy"])
            self.assertEqual(row["per_strategy"]["s1"]["signal"], "buy")
        # 不应触碰 watchlist/active 的任何字段
        self.assertNotIn("watchlist", str(report).lower())

    def test_evaluate_empty_codes_raises(self):
        with self.assertRaises(ValueError) as cm:
            evaluator.evaluate([], ["s1"], date(2026, 7, 21))
        self.assertIn("为空", str(cm.exception))

    def test_evaluate_empty_strategies_raises(self):
        with self.assertRaises(ValueError):
            evaluator.evaluate(["001"], [], date(2026, 7, 21))


class TestNarrateReview(unittest.TestCase):
    def test_llm_failure_does_not_raise(self):
        """narrate_review 失败时返回 (None, error),不抛。"""
        from stockfu.ai.client import LLMError
        with patch("stockfu.ai.client.chat", side_effect=LLMError("no key")):
            nar, err = evaluator.narrate_review(
                "001", "测试", {"s": {"signal": "buy", "total_score": 10}},
                {"signal": "buy", "avg_score": 10, "n_buy": 1, "agreement": 1.0},
                {"s": "S"},
            )
        self.assertIsNone(nar)
        self.assertIn("LLMError", err)

    def test_llm_success_returns_text(self):
        with patch("stockfu.ai.client.chat", return_value="这只股票各策略一致看多。"):
            nar, err = evaluator.narrate_review(
                "001", "测试", {"s": {"signal": "buy", "total_score": 10}},
                {"signal": "buy"}, {"s": "S"},
            )
        self.assertIsNone(err)
        self.assertIn("看多", nar)


if __name__ == "__main__":
    unittest.main()
