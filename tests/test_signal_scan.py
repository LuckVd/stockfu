"""策略评分扫描：0–100 映射、全量因子落库、逐股 LLM 与邮件筛选。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from unittest import TestCase, mock

from sqlmodel import SQLModel, Session, create_engine, select

from stockfu.models import (
    FactorSignal, IndexConstituent, LlmSignalAnalysis, StockSignalSubscription,
)
from stockfu.services import signal_scan


def _scope_factory(engine):
    @contextmanager
    def _scope():
        with Session(engine) as session:
            yield session
    return _scope


class TestScoreTo100(TestCase):
    def test_center_and_full_scale(self):
        self.assertEqual(signal_scan.score_to_100(0, 20), 50.0)
        self.assertEqual(signal_scan.score_to_100(20, 20), 100.0)
        self.assertEqual(signal_scan.score_to_100(-20, 20), 0.0)
        self.assertEqual(signal_scan.score_to_100(5, 20), 62.5)

    def test_clamps_and_risk_veto(self):
        self.assertEqual(signal_scan.score_to_100(999, 20), 100.0)
        self.assertEqual(signal_scan.score_to_100(-999, 20), 0.0)
        self.assertEqual(signal_scan.score_to_100(10, 20, risk_vetoed=True), 0.0)
        self.assertIsNone(signal_scan.score_to_100(None, 20))


class TestCurrentMemberSnapshot(TestCase):
    def test_uses_latest_complete_snapshot_not_historical_union(self):
        import stockfu.services.index_universe as index_universe

        engine = create_engine("sqlite://")
        IndexConstituent.__table__.create(engine)
        with Session(engine) as session:
            session.add_all([
                IndexConstituent(index_code="000300", asset_code="OLD",
                                 effective_from=date(2026, 1, 1)),
                IndexConstituent(index_code="000300", asset_code="NEW",
                                 effective_from=date(2026, 7, 1)),
                IndexConstituent(index_code="000905", asset_code="MID",
                                 effective_from=date(2026, 6, 1)),
            ])
            session.commit()
        with mock.patch.object(index_universe, "session_scope", _scope_factory(engine)):
            snapshot = index_universe.current_member_snapshot(date(2026, 8, 4))
            codes = index_universe.current_member_codes(date(2026, 8, 4))
        self.assertEqual(snapshot["000300"]["members"], ["NEW"])
        self.assertEqual(codes, ["MID", "NEW"])


class TestSignalScanPersistence(TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)
        self.scope = _scope_factory(self.engine)
        with Session(self.engine) as session:
            session.add(StockSignalSubscription(
                asset_code="000001", factor_mail_enabled=True, llm_enabled=True,
            ))
            session.commit()

    def test_factor_all_llm_only_enabled_and_report_filters(self):
        day = date(2026, 8, 4)
        fake_report = {
            "matrix": [
                {"code": code, "per_strategy": {
                    "s1": {"signal": "buy", "total_score": raw,
                           "confidence": 0.8, "risk_vetoed": False,
                           "factors": {"momentum": raw}},
                    "s2": {"signal": "sell", "total_score": -raw,
                           "confidence": 0.7, "risk_vetoed": False,
                           "factors": {"value": -raw}},
                }}
                for code, raw in (("000001", 10.0), ("600000", 4.0))
            ]
        }
        snapshots = {
            "000300": {"effective_from": "2026-07-24", "members": ["000001"]},
            "000905": {"effective_from": "2026-06-15", "members": ["600000"]},
        }
        llm_result = {
            "model": "deepseek-v4-flash", "score": 71.0, "summary": "偏积极",
            "reasons": ["动量较强"], "risks": ["波动"], "latency_ms": 123,
        }
        with mock.patch.object(signal_scan, "session_scope", self.scope), \
             mock.patch.object(signal_scan, "current_member_snapshot", return_value=snapshots), \
             mock.patch.object(signal_scan, "current_member_codes", return_value=["000001", "600000"]), \
             mock.patch.object(signal_scan, "_strategy_meta", return_value={
                 "s1": {"name": "策略一", "score_full": 20.0},
                 "s2": {"name": "策略二", "score_full": 20.0},
             }), \
             mock.patch("stockfu.services.evaluator.evaluate", return_value=fake_report), \
             mock.patch.object(signal_scan, "analyze_with_llm", return_value=llm_result) as llm:
            result = signal_scan.run_signal_scan(
                day, strategy_ids=["s1", "s2"], factor_enabled=True, llm_enabled=True,
            )

        self.assertEqual(result["factor_expected"], 4)
        self.assertEqual(result["factor_completed"], 4)
        self.assertEqual(result["llm_requested"], 1)
        self.assertEqual(result["llm_completed"], 1)
        llm.assert_called_once()
        self.assertEqual(llm.call_args.args[0], "000001")

        with Session(self.engine) as session:
            factors = session.exec(select(FactorSignal)).all()
            analyses = session.exec(select(LlmSignalAnalysis)).all()
        self.assertEqual(len(factors), 4)  # 邮件未开启的 600000 仍完整落因子库
        self.assertEqual(len(analyses), 1)
        scores = {(row.asset_code, row.strategy_id): row.score for row in factors}
        self.assertEqual(scores[("000001", "s1")], 75.0)
        self.assertEqual(scores[("000001", "s2")], 25.0)
        self.assertEqual([row["code"] for row in result["rows"]], ["000001", "600000"])

        with mock.patch.object(signal_scan, "session_scope", self.scope):
            subscribed = signal_scan.signal_report(
                run_id=result["run_id"], subscribed_only=True,
            )
        self.assertEqual([row["code"] for row in subscribed["rows"]], ["000001"])
        self.assertEqual(len(subscribed["rows"][0]["strategies"]), 2)
        self.assertEqual(subscribed["rows"][0]["llm"]["score"], 71.0)


class TestSignalMailHtml(TestCase):
    def test_strategies_are_separate_and_factor_switch_is_respected(self):
        from stockfu.services.signal_mail import build_signal_mail_html

        report = {
            "signal_date": "2026-08-04",
            "rows": [
                {
                    "code": "000001", "name": "平安银行",
                    "factor_mail_enabled": True, "llm_enabled": False,
                    "strategies": [
                        {"strategy_id": "s1", "strategy_name": "策略一", "score": 75, "factors": {}},
                        {"strategy_id": "s2", "strategy_name": "策略二", "score": 25, "factors": {}},
                    ], "llm": None,
                },
                {
                    "code": "600000", "name": "浦发银行",
                    "factor_mail_enabled": False, "llm_enabled": True,
                    "strategies": [
                        {"strategy_id": "hidden", "strategy_name": "不应发送", "score": 99, "factors": {}},
                    ],
                    "llm": {"status": "success", "score": 55, "model": "deepseek",
                            "summary": "中性", "reasons": [], "risks": []},
                },
            ],
        }
        page = build_signal_mail_html(report)
        self.assertIn("策略一", page)
        self.assertIn("策略二", page)
        self.assertNotIn("不应发送", page)
        self.assertIn("LLM 独立评分", page)
        self.assertIn("0–100", page)


class TestSignalScheduler(TestCase):
    def test_only_fetches_members_not_fresh_on_signal_date(self):
        from stockfu.scheduler import jobs

        day = date(2026, 8, 4)

        class _Result:
            def all(self):
                return [("A", day), ("B", date(2026, 8, 1))]

        class _Session:
            def exec(self, _stmt):
                return _Result()

        @contextmanager
        def fake_scope():
            yield _Session()

        scan = {
            "run_id": 9, "status": "success", "factor_completed": 6,
            "factor_expected": 6, "llm_completed": 0, "llm_requested": 0,
        }
        with mock.patch.object(jobs, "init_db"), \
             mock.patch.object(jobs, "session_scope", fake_scope), \
             mock.patch("stockfu.services.quote_writer.validate_ingest_date", return_value=day), \
             mock.patch("stockfu.services.index_universe.current_member_codes", return_value=["A", "B", "C"]), \
             mock.patch("stockfu.config.get_signal_factor_enabled", return_value=True), \
             mock.patch("stockfu.config.get_signal_llm_enabled", return_value=False), \
             mock.patch("stockfu.config.get_fetch_retry_count", return_value=0), \
             mock.patch("stockfu.config.get_fetch_retry_interval", return_value=1), \
             mock.patch.object(jobs, "update_index_benchmark", return_value=0), \
             mock.patch("stockfu.services.signal_scan.strategy_operator_ids", return_value=set()), \
             mock.patch.object(jobs, "_batch_fetch_today", return_value=(["B"], ["C"])) as fetch, \
             mock.patch("stockfu.services.signal_scan.run_signal_scan", return_value=scan):
            result = jobs.run_signal_pipeline(day, strategy_ids=["s1", "s2"])

        fetch.assert_called_once_with(["B", "C"], day)
        self.assertEqual(result["universe_size"], 3)
        self.assertEqual(result["quotes_already_fresh"], 1)
        self.assertEqual(result["quote_failure_codes"], ["C"])
        self.assertEqual(result["run_id"], 9)
