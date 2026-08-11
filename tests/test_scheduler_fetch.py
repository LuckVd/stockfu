"""每日抓取目标：当前自选 + 有效指数成分，不扫描历史遗留资产。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from unittest import TestCase, mock

from sqlmodel import Session, create_engine

from stockfu.models import Asset, QuoteSnapshot, SecurityMaster


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

    def test_index_codes_exclude_inactive_security_master_rows(self):
        from stockfu.scheduler import jobs

        day = date(2026, 8, 11)
        engine = create_engine("sqlite://")
        SecurityMaster.__table__.create(engine)
        with Session(engine) as session:
            session.add_all([
                SecurityMaster(code="ACTIVE", status="1"),
                SecurityMaster(code="DELIST", status="1", delist_date=day),
                SecurityMaster(code="SUSPENDED", status="0"),
                SecurityMaster(code="FUTURE", status="1", list_date=date(2026, 9, 1)),
            ])
            session.commit()

        @contextmanager
        def scope():
            with Session(engine) as session:
                yield session

        with mock.patch.object(jobs, "session_scope", scope), \
             mock.patch(
                 "stockfu.services.index_universe.current_member_codes",
                 return_value=["ACTIVE", "DELIST", "SUSPENDED", "FUTURE", "MISSING"],
             ):
            codes = jobs._current_index_fetch_codes(day, ("000300",))

        self.assertEqual(codes, ["ACTIVE", "MISSING"])

    def test_universe_fetch_skips_fresh_rows_for_resume(self):
        from stockfu.scheduler import jobs

        day = date(2026, 8, 11)
        engine = create_engine("sqlite://")
        Asset.__table__.create(engine)
        QuoteSnapshot.__table__.create(engine)
        with Session(engine) as session:
            session.add(QuoteSnapshot(
                asset_code="ACTIVE", quote_date=day, close=10.0, close_qfq=10.0,
            ))
            session.commit()

        @contextmanager
        def scope():
            with Session(engine) as session:
                yield session

        with mock.patch.object(jobs, "init_db"), \
             mock.patch.object(jobs, "session_scope", scope), \
             mock.patch.object(jobs, "_current_index_fetch_codes",
                               return_value=["ACTIVE", "PENDING"]), \
             mock.patch("stockfu.services.quote_writer.validate_ingest_date",
                        return_value=day), \
             mock.patch("stockfu.data.baostock_proxy.ensure_baostock_login",
                        return_value=True), \
             mock.patch.object(jobs, "_fetch_today_via_baostock", return_value=True) as fetch:
            result = jobs.fetch_universe_quotes(day, progress_every=0)

        fetch.assert_called_once_with("PENDING", day)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["pending"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["ok"], 1)
        self.assertEqual(result["fail"], 0)
