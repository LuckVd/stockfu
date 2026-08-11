"""V2 自选股荐股装配测试。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from unittest import TestCase, mock

from sqlmodel import Session, SQLModel, create_engine

from stockfu.models import Asset, QuoteSnapshot, SecurityMaster
from stockfu.services import v2_recommend


def _scope_factory(engine):
    @contextmanager
    def scope():
        with Session(engine) as session:
            yield session
    return scope


class TestV2WatchlistAssembly(TestCase):
    def test_stock_filter_excludes_etf_and_inactive(self):
        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            session.add_all([
                Asset(code="600000", market="cn", asset_type="stock", is_watch=True),
                Asset(code="510300", market="cn", asset_type="fund_etf", is_watch=True),
                Asset(code="600001", market="cn", asset_type="stock", is_watch=True),
                Asset(code="600002", market="cn", asset_type="stock", is_watch=False),
            ])
            session.add(SecurityMaster(code="600001", status="0"))
            session.commit()
        with mock.patch.object(v2_recommend, "session_scope", _scope_factory(engine)):
            self.assertEqual(
                v2_recommend.watchlist_stock_codes(date(2026, 8, 11)), ["600000"]
            )

    def test_quote_coverage_reports_missing(self):
        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            session.add(QuoteSnapshot(asset_code="600000", quote_date=date(2026, 8, 11)))
            session.commit()
        with mock.patch.object(v2_recommend, "session_scope", _scope_factory(engine)):
            result = v2_recommend.quote_coverage(
                ["600000", "600001"], date(2026, 8, 11)
            )
        self.assertEqual(
            result, {"expected": 2, "present": 1, "missing": ["600001"]}
        )

    def test_rank_rows_keeps_strategy_scores_and_adds_consensus(self):
        rows = v2_recommend._rank_rows([
            {"code": "B", "scores": {"a": {"score": 60, "status": "tradable"}}},
            {"code": "A", "scores": {
                "a": {"score": 80, "status": "tradable"},
                "b": {"score": 40, "status": "tradable"},
            }},
        ])
        self.assertEqual([row["code"] for row in rows], ["A", "B"])
        self.assertEqual(rows[0]["mean_score"], 60.0)
        self.assertEqual(rows[0]["n_bullish"], 1)
        self.assertEqual(rows[0]["recommendation"], "优先关注")
