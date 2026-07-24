"""分享导出数据完整性：只验证卡片实际依赖的行情日期与三表路由。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from unittest import TestCase, mock

from sqlmodel import SQLModel, Session, create_engine

import stockfu.models  # noqa: F401  注册 metadata
from stockfu.models import Asset, EtfQuoteDaily, IndexQuoteDaily, QuoteSnapshot


class TestShareIntegrity(TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.day = date(2026, 7, 24)

    def tearDown(self):
        self.session.close()

    @contextmanager
    def _scope(self):
        yield self.session

    def _add_indices(self):
        for code in ("sh000001", "sz399006", "sh000688"):
            self.session.add(IndexQuoteDaily(asset_code=code, quote_date=self.day, close=3000))
        self.session.commit()

    def test_readiness_routes_etf_to_etf_table(self):
        """ETF 新表当天有数据时，旧 quote_snapshot 的陈旧孤儿行不能阻止导出。"""
        self.session.add(Asset(code="510300", is_watch=True, asset_type="fund_etf"))
        self.session.add(EtfQuoteDaily(asset_code="510300", quote_date=self.day, close=4))
        self.session.add(QuoteSnapshot(asset_code="510300", quote_date=date(2026, 7, 23), close=99))
        self._add_indices()

        with mock.patch("stockfu.db.session_scope", self._scope):
            from stockfu.services.share import export_readiness
            result = export_readiness(self.day)

        self.assertTrue(result["ok"])
        self.assertEqual(result["stale"], [])

    def test_readiness_reports_stale_share_component(self):
        self.session.add(Asset(code="600519", is_watch=True))
        self.session.add(QuoteSnapshot(asset_code="600519", quote_date=date(2026, 7, 23), close=100))
        self._add_indices()

        with mock.patch("stockfu.db.session_scope", self._scope):
            from stockfu.services.share import export_readiness
            result = export_readiness(self.day)

        self.assertFalse(result["ok"])
        self.assertEqual(result["stale"], [{"code": "600519", "quote_date": "2026-07-23"}])

