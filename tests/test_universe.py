"""宇宙规则纯逻辑单测(不依赖完整 DB 时测 board/limit/eligible 契约)。"""
from __future__ import annotations

import unittest
from datetime import date
import sys
from unittest.mock import MagicMock, patch

from stockfu.services.universe import (
    DayFlags, UniverseContext, UniverseRules, board_of_code, limit_pct_for,
)
from stockfu.services.index_universe import (
    HISTORICAL_INDEX_CODES, _month_starts, member_on, normalize_code,
    fetch_baostock_index_snapshot, parse_sina_corp_index_history,
)


class TestBoardAndLimit(unittest.TestCase):
    def test_board_of_code(self):
        self.assertEqual(board_of_code("600519"), "main")
        self.assertEqual(board_of_code("300750"), "chinext")
        self.assertEqual(board_of_code("688981"), "star")
        self.assertEqual(board_of_code("830799"), "bse")

    def test_limit_pct(self):
        self.assertEqual(limit_pct_for("main"), 10.0)
        self.assertEqual(limit_pct_for("chinext"), 20.0)
        self.assertEqual(limit_pct_for("main", is_st=True), 5.0)


class TestEligibleOn(unittest.TestCase):
    def _ctx(self, codes, list_dates, first_quotes=None, rules=None):
        ctx = UniverseContext(codes=codes, rules=rules or UniverseRules())
        for c, ld in list_dates.items():
            m = MagicMock()
            m.list_date = ld
            m.delist_date = None
            m.board = board_of_code(c)
            ctx.master[c] = m
        ctx.first_quote = first_quotes or list_dates
        return ctx

    def test_requires_day_flags(self):
        ctx = self._ctx(["600519"], {"600519": date(2001, 8, 27)})
        with self.assertRaises(ValueError):
            ctx.eligible_on(date(2025, 1, 2), None)

    def test_blocks_before_list_and_cooling(self):
        ctx = self._ctx(
            ["NEW", "OLD"],
            {"NEW": date(2025, 6, 1), "OLD": date(2000, 1, 1)},
            rules=UniverseRules(min_list_days=60),
        )
        flags = {
            "NEW": DayFlags(has_row=True, is_st=False, trade_status=1),
            "OLD": DayFlags(has_row=True, is_st=False, trade_status=1),
        }
        # NEW 上市仅 10 天
        u = ctx.eligible_on(date(2025, 6, 11), flags)
        self.assertNotIn("NEW", u)
        self.assertIn("OLD", u)
        # NEW 冷静期满
        u2 = ctx.eligible_on(date(2025, 8, 15), flags)
        self.assertIn("NEW", u2)

    def test_exclude_st_and_suspend(self):
        ctx = self._ctx(["A", "B", "C"], {
            "A": date(2010, 1, 1), "B": date(2010, 1, 1), "C": date(2010, 1, 1),
        })
        flags = {
            "A": DayFlags(has_row=True, is_st=True, trade_status=1),
            "B": DayFlags(has_row=True, is_st=False, trade_status=0),
            "C": DayFlags(has_row=True, is_st=False, trade_status=1),
        }
        u = ctx.eligible_on(date(2025, 1, 2), flags)
        self.assertEqual(u, {"C"})

    def test_historical_membership_is_point_in_time(self):
        rules = UniverseRules(index_codes=("000300",), min_list_days=0)
        ctx = self._ctx(["IN", "OUT"], {
            "IN": date(2010, 1, 1), "OUT": date(2010, 1, 1),
        }, rules=rules)
        ctx.memberships = {"IN": [(date(2020, 1, 1), date(2020, 6, 1))]}
        flags = {"IN": DayFlags(has_row=True), "OUT": DayFlags(has_row=True)}
        self.assertEqual(ctx.eligible_on(date(2020, 5, 29), flags), {"IN"})
        self.assertEqual(ctx.eligible_on(date(2020, 6, 1), flags), set())


class TestIndexMembershipIntervals(unittest.TestCase):
    def test_default_universe_excludes_unfinished_indices(self):
        self.assertEqual(HISTORICAL_INDEX_CODES, ("000300", "000905"))

    def test_right_boundary_is_exclusive(self):
        spans = [(date(2020, 1, 1), date(2020, 6, 1))]
        self.assertTrue(member_on(spans, date(2020, 5, 29)))
        self.assertFalse(member_on(spans, date(2020, 6, 1)))

    def test_monthly_mirror_boundaries(self):
        self.assertEqual(
            _month_starts(date(2025, 4, 18), date(2025, 6, 1)),
            [date(2025, 4, 1), date(2025, 5, 1), date(2025, 6, 1)],
        )

    def test_official_adjustment_codes_can_be_normalized(self):
        self.assertEqual({normalize_code(v) for v in ("688001", 688002.0)},
                         {"688001", "688002"})

    def test_import_snapshot_strips_blacklisted_index_codes(self):
        """baostock 早期快照会把指数代码误录为成分；import 必须剔除，
        且不能误伤与上证综指撞号的 000001（平安银行）。"""
        from contextlib import contextmanager
        from sqlmodel import Session, create_engine, select
        import stockfu.services.index_universe as iu
        from stockfu.models import IndexConstituent

        engine = create_engine("sqlite://")
        IndexConstituent.__table__.create(engine)

        @contextmanager
        def fake_session():
            with Session(engine) as s:
                yield s

        members = ["600000", "000905", "000016", "000852", "000688", "000001"]
        with patch.object(iu, "session_scope", fake_session):
            iu.import_snapshot("000905", members, effective_from=date(2010, 1, 1),
                               source="test", source_ref="t")
            with Session(engine) as s:
                codes = set(s.exec(select(IndexConstituent.asset_code)).all())

        for idx in ("000905", "000016", "000852", "000688"):
            self.assertNotIn(idx, codes)
        self.assertIn("000001", codes)   # 平安银行，与上证综指撞号但合法个股
        self.assertIn("600000", codes)

    def test_sina_corp_page_keeps_removed_index_memberships(self):
        html = """
        <table><tr><th>名称</th></tr>
        <tr><td><div>中证1000</div></td><td><div>000852</div></td>
            <td><div>2014-10-17</div></td><td><div>2019-12-16</div></td></tr>
        <tr><td><div>沪深300</div></td><td><div>000300</div></td>
            <td><div>2014-10-17</div></td><td><div></div></td></tr>
        </table>
        """
        self.assertEqual(parse_sina_corp_index_history(html), [
            (date(2014, 10, 17), date(2019, 12, 16)),
        ])

    def test_baostock_index_query_relogs_and_retries_same_day(self):
        failed = MagicMock(error_code="10001001", error_msg="用户未登录")
        succeeded = MagicMock(error_code="0", fields=["code"])
        succeeded.next.side_effect = [True] * 500 + [False]
        succeeded.get_row_data.side_effect = [
            [f"sh.{600000 + n:06d}"] for n in range(500)
        ]
        fake_bs = MagicMock(query_zz500_stocks=MagicMock(side_effect=[failed, succeeded]))
        with patch.dict(sys.modules, {"baostock": fake_bs}), patch(
            "stockfu.data.baostock_proxy.ensure_baostock_login", return_value=True,
        ) as login:
            self.assertEqual(fetch_baostock_index_snapshot("000905", date(2009, 5, 14)),
                             {f"{600000 + n:06d}" for n in range(500)})
        self.assertEqual(login.call_args_list[0].args, ())
        self.assertEqual(login.call_args_list[1].kwargs, {"force": True})


if __name__ == "__main__":
    unittest.main()
