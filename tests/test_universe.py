"""宇宙规则纯逻辑单测(不依赖完整 DB 时测 board/limit/eligible 契约)。"""
from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import MagicMock

from stockfu.services.universe import (
    DayFlags, UniverseContext, UniverseRules, board_of_code, limit_pct_for,
)
from stockfu.services.index_universe import member_on


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
    def test_right_boundary_is_exclusive(self):
        spans = [(date(2020, 1, 1), date(2020, 6, 1))]
        self.assertTrue(member_on(spans, date(2020, 5, 29)))
        self.assertFalse(member_on(spans, date(2020, 6, 1)))


if __name__ == "__main__":
    unittest.main()
