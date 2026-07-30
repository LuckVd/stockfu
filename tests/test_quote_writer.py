"""quote_writer 收口 + 日期权威单测。

验证：
  - validate_ingest_date：未来日 / 当日未收盘 / 非交易日 报错；合法历史交易日通过；
    str 与 date 均接受。
  - latest_trade_date_on_or_before：按日历回退；离线按周末回退。
  - upsert_quote_snapshot：cap_date 硬丢弃未来 bar；PATCH_STATUS 遇新行升级为全量插入。
  - upsert_etf_daily / upsert_index_daily：cap_date 守卫。
"""
from __future__ import annotations

import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest import mock

from sqlmodel import Session, SQLModel, create_engine, select


def _bar(d, close):
    return SimpleNamespace(date=d, open=close, high=close, low=close, close=close,
                           volume=1, amount=1, pct_chg=1.0, trade_status=1, is_st=0,
                           pe=10, pb=1, turnover=1)


class TestValidateIngestDate(unittest.TestCase):
    def _cal(self, *days):
        return {date.fromisoformat(d) for d in days}

    def test_future_date_rejected(self):
        with mock.patch("stockfu.services.snapshot._trade_calendar",
                        return_value=self._cal("2026-07-22")):
            with self.assertRaises(ValueError):
                from stockfu.services.quote_writer import validate_ingest_date
                validate_ingest_date("2026-07-23", now=datetime(2026, 7, 22, 9, 0))

    def test_today_not_closed_rejected(self):
        cal = self._cal("2026-07-22")
        with mock.patch("stockfu.services.snapshot._trade_calendar", return_value=cal):
            from stockfu.services.quote_writer import validate_ingest_date
            with self.assertRaises(ValueError):
                validate_ingest_date("2026-07-22", now=datetime(2026, 7, 22, 9, 0))

    def test_today_after_close_accepted(self):
        cal = self._cal("2026-07-22")
        with mock.patch("stockfu.services.snapshot._trade_calendar", return_value=cal):
            from stockfu.services.quote_writer import validate_ingest_date
            d = validate_ingest_date("2026-07-22", now=datetime(2026, 7, 22, 17, 0))
            self.assertEqual(d, date(2026, 7, 22))

    def test_today_accepted_at_default_cutoff(self):
        cal = self._cal("2026-07-22")
        with mock.patch("stockfu.services.snapshot._trade_calendar", return_value=cal):
            from stockfu.services.quote_writer import validate_ingest_date
            self.assertEqual(
                validate_ingest_date("2026-07-22", now=datetime(2026, 7, 22, 15, 30)),
                date(2026, 7, 22),
            )

    def test_non_trading_day_rejected(self):
        cal = self._cal("2026-07-22")  # 2026-07-25 周六不在日历
        with mock.patch("stockfu.services.snapshot._trade_calendar", return_value=cal):
            from stockfu.services.quote_writer import validate_ingest_date
            with self.assertRaises(ValueError):
                validate_ingest_date("2026-07-25", now=datetime(2026, 7, 25, 17, 0))

    def test_past_trading_day_accepted(self):
        cal = self._cal("2026-07-20", "2026-07-21", "2026-07-22")
        with mock.patch("stockfu.services.snapshot._trade_calendar", return_value=cal):
            from stockfu.services.quote_writer import validate_ingest_date
            d = validate_ingest_date("2026-07-20", now=datetime(2026, 7, 22, 17, 0))
            self.assertEqual(d, date(2026, 7, 20))

    def test_string_and_date_both_accepted(self):
        cal = self._cal("2026-07-22")
        with mock.patch("stockfu.services.snapshot._trade_calendar", return_value=cal):
            from stockfu.services.quote_writer import validate_ingest_date
            self.assertEqual(validate_ingest_date(date(2026, 7, 22),
                             now=datetime(2026, 7, 22, 17, 0)), date(2026, 7, 22))
            self.assertEqual(validate_ingest_date("2026-07-22",
                             now=datetime(2026, 7, 22, 17, 0)), date(2026, 7, 22))


class TestLatestTradeDateOnOrBefore(unittest.TestCase):
    def test_walks_back_to_calendar_day(self):
        cal = {date(2026, 7, 17), date(2026, 7, 20), date(2026, 7, 21), date(2026, 7, 22)}
        with mock.patch("stockfu.services.snapshot._trade_calendar", return_value=cal):
            from stockfu.services.quote_writer import latest_trade_date_on_or_before
            self.assertEqual(latest_trade_date_on_or_before("2026-07-19"), date(2026, 7, 17))
            self.assertEqual(latest_trade_date_on_or_before("2026-07-22"), date(2026, 7, 22))

    def test_offline_weekend_fallback(self):
        with mock.patch("stockfu.services.snapshot._trade_calendar", return_value=None):
            from stockfu.services.quote_writer import latest_trade_date_on_or_before
            # 2026-07-25 周六 → 回退到 07-24 周五
            self.assertEqual(latest_trade_date_on_or_before(date(2026, 7, 25)),
                             date(2026, 7, 24))


class _DbTest(unittest.TestCase):
    def setUp(self):
        import stockfu.models  # noqa: F401  注册所有表到 metadata
        self.engine = create_engine("sqlite:///:memory:")
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self):
        self.session.close()


class TestUpsertQuoteSnapshot(_DbTest):
    def test_cap_date_drops_future_bars(self):
        from stockfu.models import QuoteSnapshot
        from stockfu.services.quote_writer import (
            QuotePayload, WritePolicy, upsert_quote_snapshot,
        )
        payload = {
            date(2026, 7, 21): QuotePayload(qfq=_bar(date(2026, 7, 21), 10)),
            date(2026, 7, 22): QuotePayload(qfq=_bar(date(2026, 7, 22), 11)),
        }
        n = upsert_quote_snapshot(self.session, "600519", payload,
                                  policy=WritePolicy.FULL_QFQ, cap_date="2026-07-21")
        self.assertEqual(n, 1)  # 07-22 被 cap 丢弃
        rows = self.session.exec(select(QuoteSnapshot)).all()
        self.assertEqual(sorted(r.quote_date for r in rows), [date(2026, 7, 21)])

    def test_patch_status_on_new_row_upgrades_to_full(self):
        from stockfu.models import QuoteSnapshot
        from stockfu.services.quote_writer import (
            QuotePayload, WritePolicy, upsert_quote_snapshot,
        )
        payload = {date(2026, 7, 21): QuotePayload(
            qfq=_bar(date(2026, 7, 21), 10), policy=WritePolicy.PATCH_STATUS)}
        n = upsert_quote_snapshot(self.session, "600519", payload,
                                  policy=WritePolicy.PATCH_STATUS, cap_date="2026-07-22")
        self.assertEqual(n, 1)
        snap = self.session.exec(select(QuoteSnapshot)).first()
        self.assertEqual(snap.close, 10)
        self.assertEqual(snap.close_qfq, 10)  # 新行被全量写入

    def test_merge_adj_writes_three_adjustments_same_date(self):
        from stockfu.models import QuoteSnapshot
        from stockfu.services.quote_writer import (
            QuotePayload, WritePolicy, upsert_quote_snapshot,
        )
        d = date(2026, 7, 21)
        payload = {d: QuotePayload(qfq=_bar(d, 10), raw=_bar(d, 12), hfq=_bar(d, 8))}
        upsert_quote_snapshot(self.session, "600519", payload,
                              policy=WritePolicy.MERGE_ADJ, cap_date="2026-07-21")
        snap = self.session.exec(select(QuoteSnapshot)).first()
        self.assertEqual(snap.close_qfq, 10)
        self.assertEqual(snap.close_raw, 12)
        self.assertEqual(snap.close_hfq, 8)


class TestUpsertEtfAndIndexCap(_DbTest):
    def _rows(self):
        return [
            {"asset_code": "510300", "quote_date": date(2026, 7, 21), "open": 4,
             "high": 4, "low": 4, "close": 4, "pct_chg": 1, "volume": 1, "amount": 1},
            {"asset_code": "510300", "quote_date": date(2026, 7, 22), "open": 5,
             "high": 5, "low": 5, "close": 5, "pct_chg": 1, "volume": 1, "amount": 1},
        ]

    def test_etf_cap_drops_future(self):
        from stockfu.models import EtfQuoteDaily
        from stockfu.services.quote_writer import upsert_etf_daily
        n = upsert_etf_daily(self.session, "510300", self._rows(), cap_date="2026-07-21")
        self.assertEqual(n, 1)
        rows = self.session.exec(select(EtfQuoteDaily)).all()
        self.assertEqual(sorted(r.quote_date for r in rows), [date(2026, 7, 21)])

    def test_index_cap_drops_future_and_skips_existing(self):
        from stockfu.models import IndexQuoteDaily
        from stockfu.services.quote_writer import upsert_index_daily
        rows = [{"asset_code": "sh000001", "quote_date": date(2026, 7, 21), "close": 3000},
                {"asset_code": "sh000001", "quote_date": date(2026, 7, 22), "close": 3100}]
        n = upsert_index_daily(self.session, "sh000001", rows, cap_date="2026-07-21")
        self.assertEqual(n, 1)  # 07-22 被 cap 丢弃
        # 再写一次同日 → skip existing，不增
        n2 = upsert_index_daily(self.session, "sh000001",
                                [{"asset_code": "sh000001", "quote_date": date(2026, 7, 21),
                                  "close": 9999}], cap_date="2026-07-21", overwrite=False)
        self.assertEqual(n2, 0)
        snap = self.session.exec(select(IndexQuoteDaily)).first()
        self.assertEqual(snap.close, 3000)  # 未被覆盖


if __name__ == "__main__":
    unittest.main()
