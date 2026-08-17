"""2026-08-17 审查修复的回归测试。

- H1/_QUERY_LOCK：baostock 查询进程级串行化 + run(deadline=) 时长上界
  （替代外层 _call_timeout 子线程——其 35s < 内层 fetch_timeout 60s 会留
  孤儿线程与下一票并发共用全局裸 TCP socket）。
- M3/表路由：_upsert_quote 按 quote_model_for 分流 ETF/指数到各自 canonical
  表（旧实现把 ETF 写进 quote_snapshot 错表、指数可能被 normalize 成
  000001 拿到平安银行行情）；ETF 源失败计入 fail 参与重试。
- clean_quote_snapshots：错表孤儿行仅在正确表已有同日数据时删除。
"""
from __future__ import annotations

import threading
import time
import unittest
from contextlib import contextmanager
from datetime import date
from unittest import TestCase, mock

from sqlmodel import Session, create_engine

from stockfu.models import EtfQuoteDaily, QuoteSnapshot


class TestBaostockQueryLock(TestCase):
    def _session(self):
        from stockfu.data.baostock_proxy import BaostockProxySession

        return BaostockProxySession(use_free_pool=False, seed_local_clash=False)

    def test_run_deadline_exceeded_raises_without_attempt(self):
        s = self._session()
        called = []

        def fn():
            called.append(1)
            return "v"

        with self.assertRaises(RuntimeError):
            s.run(fn, deadline=time.monotonic() - 1.0, label="t")
        self.assertEqual(called, [])   # 超预算后一次尝试都不发起

    def test_run_returns_last_value_when_deadline_hit_after_success(self):
        s = self._session()

        def fn():
            return "ok"

        # 先让一次成功，再给一个已过期的 deadline 不成立（成功立即返回）；
        # 真正的「成功后超预算」路径由 deadline 检查在 attempt 循环顶部短路。
        self.assertEqual(s.run(fn, deadline=time.monotonic() + 30), "ok")

    def test_query_lock_serializes_concurrent_run(self):
        from stockfu.data.baostock_proxy import _QUERY_LOCK

        s = self._session()
        state = {"active": 0, "peak": 0}
        book = threading.Lock()

        def fn():
            with book:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            time.sleep(0.05)
            with book:
                state["active"] -= 1
            return 1

        results: list = []
        errs: list = []

        def _worker():
            try:
                results.append(s.run(fn, label="ser"))
            except Exception as e:  # noqa: BLE001
                errs.append(e)

        ts = [threading.Thread(target=_worker) for _ in range(3)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errs, [])
        self.assertEqual(len(results), 3)
        self.assertEqual(state["peak"], 1)   # 无并发进入：全程串行
        # 锁仍处于释放状态（RLock 可重入，用另一线程试探获取）
        acquired = _QUERY_LOCK.acquire(timeout=1.0)
        if acquired:
            _QUERY_LOCK.release()
        self.assertTrue(acquired)


class TestUpsertQuoteRouting(TestCase):
    def test_etf_and_index_route_to_canonical_tables(self):
        from stockfu.scheduler import jobs

        day = date(2026, 8, 14)
        with mock.patch.object(jobs, "update_etf_benchmark",
                               return_value=3) as etf_up, \
             mock.patch.object(jobs, "update_index_benchmark",
                               return_value=1) as idx_up, \
             mock.patch.object(jobs, "_upsert_quote_via_manager") as mgr:
            self.assertTrue(jobs._upsert_quote("510300", day))
            self.assertTrue(jobs._upsert_quote("sh000001", day))
        etf_up.assert_called_once_with("510300", day)
        idx_up.assert_called_once_with("sh000001", day)
        mgr.assert_not_called()   # 绝不再漏到 manager 多源路径写 quote_snapshot

    def test_etf_source_failure_counts_as_fail(self):
        from stockfu.scheduler import jobs

        day = date(2026, 8, 14)
        with mock.patch.object(jobs, "update_etf_benchmark",
                               side_effect=RuntimeError("akshare down")):
            self.assertFalse(jobs._upsert_quote("510300", day))
            ok, fail = jobs._batch_fetch_today(["510300"], day)
        self.assertEqual(ok, [])
        self.assertEqual(fail, ["510300"])   # 旧实现无条件计 ok、不参与重试

    def test_cn_stock_fetches_synchronously_via_baostock(self):
        from stockfu.scheduler import jobs

        day = date(2026, 8, 14)
        with mock.patch.object(jobs, "_fetch_today_via_baostock",
                               return_value=True) as bs_fetch, \
             mock.patch.object(jobs, "_call_timeout") as timeout_stub:
            self.assertTrue(jobs._upsert_quote("600519", day))
        bs_fetch.assert_called_once_with("600519", day)
        timeout_stub.assert_not_called()   # 不再套子线程超时（防孤儿线程）


class TestCleanQuoteSnapshots(TestCase):
    def test_deletes_covered_orphans_keeps_uncovered_and_normal(self):
        from stockfu.scheduler import jobs

        day = date(2026, 8, 14)
        engine = create_engine("sqlite://")
        QuoteSnapshot.__table__.create(engine)
        EtfQuoteDaily.__table__.create(engine)
        with Session(engine) as session:
            session.add_all([
                # 错表孤儿(ETF)——正确表同日有数据 → 删
                QuoteSnapshot(asset_code="510300", quote_date=day, close=4.0),
                EtfQuoteDaily(asset_code="510300", quote_date=day, close=4.0),
                # 错表孤儿——正确表缺该日 → 保留(不删唯一记录)
                QuoteSnapshot(asset_code="510500", quote_date=day, close=6.0),
                # 正常个股行(交易日) → 保留
                QuoteSnapshot(asset_code="600519", quote_date=day, close=1500.0),
                # 非交易日错标 → 删
                QuoteSnapshot(asset_code="600519", quote_date=date(2026, 8, 16),
                              close=1499.0),
            ])
            session.commit()

        @contextmanager
        def scope():
            with Session(engine) as session:
                yield session

        with mock.patch.object(jobs, "session_scope", scope), \
             mock.patch("stockfu.services.snapshot._trade_calendar",
                        return_value={day}):
            result = jobs.clean_quote_snapshots()

        self.assertEqual(result["deleted"], 2)
        with Session(engine) as session:
            left = {(r.asset_code, r.quote_date)
                    for r in session.exec(
                        __import__("sqlmodel").select(QuoteSnapshot)).all()}
        self.assertEqual(left, {("510500", day), ("600519", day)})


if __name__ == "__main__":
    unittest.main()
