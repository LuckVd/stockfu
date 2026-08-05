"""定时邮件的数据刷新必须与个股抓取链路隔离。"""
from __future__ import annotations

from datetime import date
from unittest import TestCase, mock


class TestMailFetch(TestCase):
    def test_only_refreshes_market_and_sectors(self):
        from stockfu.scheduler import jobs

        day = date(2026, 7, 29)
        readiness = {"ok": True, "stale": []}
        with mock.patch.object(jobs, "init_db"), \
             mock.patch("stockfu.services.quote_writer.validate_ingest_date", return_value=day), \
             mock.patch.object(jobs, "update_index_benchmark", side_effect=[1, 2, 3]) as indices, \
             mock.patch.object(jobs, "_batch_fetch_today") as stock_quotes, \
             mock.patch.object(jobs, "_call_timeout") as timeout, \
             mock.patch("stockfu.services.backfill.refresh_sector_pulse_today",
                        return_value={"same_day": 90}), \
             mock.patch("stockfu.services.composite.compute_all", return_value={}) as composite, \
             mock.patch("stockfu.services.share.export_readiness", return_value=readiness):
            timeout.side_effect = lambda fn, *_args, **_kwargs: fn()
            result = jobs.run_mail_fetch(day)

        self.assertEqual(indices.call_count, 3)
        self.assertEqual([c.args[0] for c in indices.call_args_list],
                         ["sh000001", "sz399006", "sh000688"])
        stock_quotes.assert_not_called()
        composite.assert_called_once_with([], day)
        self.assertEqual(timeout.call_args_list[0].args[1], 300)
        self.assertEqual(result["sector_pulse"], {"same_day": 90})
        self.assertTrue(result["export_ready"])
