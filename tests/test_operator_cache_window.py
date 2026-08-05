"""回测算子缓存「滚动分块预载」单测(无 DB)。

begin_run_cache 只预载 [start, start+WINDOW] 首块,prefetch_cache 逐日消费、到窗口尾部
提前量内自动补下一块 → 长区间峰值内存有界(≈ 窗口×每日行数),全程内存 hit 无 miss。
依赖合成算子数据经 monkeypatch operator_cache.load_operator_results_range 注入;
_ensure_op_meta 的 DB 读(_load_operator_meta)也 monkeypatch 掉。
"""
from __future__ import annotations

import math
import unittest
from contextlib import ExitStack
from datetime import date, timedelta
from unittest.mock import patch

from stockfu.ai.operators import runner
from stockfu.ai.operators.runner import compile_strategy

CODES = ["000001.SZ", "000002.SZ"]
OP_ID = "dividend_yield"


def _trading_days(start: date, n: int) -> list[date]:
    out = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


class TestRollingRunCache(unittest.TestCase):
    WINDOW = 100
    LOOKAHEAD = 10
    N_DAYS = 520

    def setUp(self):
        self._days = _trading_days(date(2020, 1, 2), self.N_DAYS)
        # 合成全量算子缓存:{as_of: {op_id: {code: pack}}};pack=(signal, score, conf, value, veto)
        self.full = {
            d: {OP_ID: {c: ("hold", 8.0, 0.8, None, False) for c in CODES}}
            for d in self._days
        }
        self.start = self._days[0]
        self.end = self._days[-1]
        self.loader_calls = 0

    def _fake_loader(self, codes, start, end, op_fps, op_types=None):
        """替代 load_operator_results_range:按 [start,end] 切合成数据(记录调用次数)。"""
        self.loader_calls += 1
        lo = start if isinstance(start, date) else date.fromisoformat(str(start)[:10])
        hi = end if isinstance(end, date) else date.fromisoformat(str(end)[:10])
        return {d: by_op for d, by_op in self.full.items() if lo <= d <= hi}

    def _make(self) -> "runner.CompiledStrategy":
        # 先全量发现算子,避免前面用例只导入单个算子模块(如 macd_cross)留下
        # REGISTRY 非空但缺 dividend_yield 的中间态(compile_strategy 的 if not REGISTRY 会跳过发现)。
        from stockfu.ai.operators.registry import discover_and_register
        discover_and_register()
        text = (
            "version: 1\n"
            "name: t\n"
            "operators:\n"
            "  - {id: dividend_yield, type: math, weight: 1.0}\n"
            "position:\n"
            "  mode: continuous\n"
        )
        return compile_strategy(text, strategy_id="t")

    def _patched(self):
        stack = ExitStack()
        for p in (
            patch.object(runner, "RUN_CACHE_WINDOW_DAYS", self.WINDOW),
            patch.object(runner, "RUN_CACHE_LOOKAHEAD_DAYS", self.LOOKAHEAD),
            patch("stockfu.ai.operator_cache.load_operator_results_range",
                  side_effect=self._fake_loader),
            patch.object(runner, "_load_operator_meta", return_value=1),
        ):
            stack.enter_context(p)
        return stack

    def _run_days(self, cs, days):
        peak = 0
        all_hit = True
        for d in days:
            prefill = cs.prefetch_cache(CODES, d)
            if len(prefill) != len(CODES):
                all_hit = False
            peak = max(peak, len(cs._run_op_cache))
        return all_hit, peak

    def test_rolling_peak_bounded_and_no_miss(self):
        with self._patched():
            cs = self._make()
            cs.begin_run_cache(CODES, self.start, self.end)
            all_hit, peak = self._run_days(cs, self._days)
        self.assertTrue(all_hit, "逐日 prefill 必须全部内存 hit(无 miss 计算)")
        self.assertLessEqual(
            peak, self.WINDOW + self.LOOKAHEAD + 1,
            "缓存日数峰值须有界(窗口 + 提前量)")
        self.assertGreaterEqual(self.loader_calls, 2, "必须发生滚动补块")
        span_days = (self.end - self.start).days   # 窗口按日历日推进
        self.assertLessEqual(
            self.loader_calls, 1 + math.ceil(span_days / self.WINDOW),
            "补块次数不应超过窗口数分片(日历日口径)")

    def test_tail_converged_no_reload(self):
        with self._patched():
            cs = self._make()
            cs.begin_run_cache(CODES, self.start, self.end)
            self._run_days(cs, self._days)
            after = self.loader_calls
        self.assertGreaterEqual(cs._run_window_end, self.end, "窗口尾部须收敛到 end")
        with self._patched():
            cs.prefetch_cache(CODES, self.end)   # 尾部之后再调:不得再触发分块加载
        self.assertEqual(self.loader_calls, after)

    def test_small_range_single_chunk_identical(self):
        short = self._days[:50]                  # 范围 ≤ 窗口 → 单块全量 = 旧行为
        with self._patched():
            cs = self._make()
            cs.begin_run_cache(CODES, short[0], short[-1])
            all_hit, peak = self._run_days(cs, short)
        self.assertTrue(all_hit)
        self.assertEqual(self.loader_calls, 1, "短区间应只有首块一次加载")
        self.assertLessEqual(peak, len(short), "峰值 = 全量(等价旧行为)")

    def test_end_run_cache_clears_window_state(self):
        with self._patched():
            cs = self._make()
            cs.begin_run_cache(CODES, self.start, self.end)
            cs.end_run_cache()
        self.assertIsNone(cs._run_op_cache)
        self.assertIsNone(cs._run_codes)
        self.assertIsNone(cs._run_end)
        self.assertIsNone(cs._run_window_end)


if __name__ == "__main__":
    unittest.main()
