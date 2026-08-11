"""每日抓取只应覆盖当前自选资产，不应扫描历史回测资产。"""
from __future__ import annotations

from contextlib import contextmanager
from unittest import TestCase, mock

from sqlmodel import Session, create_engine

from stockfu.models import Asset


class TestScheduledFetchTargets(TestCase):
    def test_current_watch_codes_exclude_historical_assets(self):
        from stockfu.scheduler import jobs

        engine = create_engine("sqlite://")
        Asset.__table__.create(engine)
        with Session(engine) as session:
            session.add_all([
                Asset(code="600519", name="贵州茅台", is_watch=True),
                Asset(code="000508", name="琼民源Ａ", is_watch=False),
                Asset(code="600625", name="PT水仙", is_watch=False),
                Asset(code="510300", name="沪深300ETF", is_watch=True,
                      asset_type="fund_etf"),
            ])
            session.commit()

        @contextmanager
        def scope():
            with Session(engine) as session:
                yield session

        with mock.patch.object(jobs, "session_scope", scope):
            codes = jobs._current_watch_codes()

        self.assertEqual(set(codes), {"600519", "510300"})
        self.assertNotIn("000508", codes)
        self.assertNotIn("600625", codes)
