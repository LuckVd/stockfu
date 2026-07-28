"""公司行为账本：送转必须保留整手/FIFO，并拒绝冲突源事件。"""
from __future__ import annotations

import unittest
from datetime import date

from stockfu.backtest.engine import Position, VirtualAccount, _canonical_dividend_rows, settle_dividends
from stockfu.data.base import DividendEventDTO
from stockfu.models import DividendEvent
from stockfu.services.dividend import (
    CorporateActionConflictError, _canonical_events, _summarize_corporate_action_rows,
)


class TestStockDividendSettlement(unittest.TestCase):
    def test_stock_dividend_doubles_shares_cost_and_lots(self):
        acct = VirtualAccount(1_000_000)
        pos = Position(shares=300, avg_cost=10.0,
                       lots=[(100, date(2024, 1, 2)), (200, date(2024, 2, 2))])
        acct.positions["002024"] = pos

        rec = acct.adjust_for_stock_dividend("002024", 1.0, date(2024, 6, 1))

        self.assertEqual(rec["shares_before"], 300)
        self.assertEqual(rec["shares_after"], 600)
        self.assertEqual(pos.shares, 600)
        self.assertEqual(pos.avg_cost, 5.0)
        self.assertEqual(pos.lots, [(200, date(2024, 1, 2)), (400, date(2024, 2, 2))])
        # 除权前 300×10 = 除权后 600×5；账户权益不产生机械跳变。
        self.assertEqual(acct.equity({"002024": 5.0}), 1_003_000.0)

    def test_cash_is_credited_before_stock_dividend(self):
        acct = VirtualAccount(0)
        acct.positions["002024"] = Position(
            shares=100, avg_cost=10.0, lots=[(100, date(2023, 1, 1))]
        )
        # 10派1、10转10：现金应按旧 100 股计，再变为 200 股。
        cash = acct.credit_dividend("002024", 0.1, date(2024, 6, 1))
        stock = acct.adjust_for_stock_dividend("002024", 1.0, date(2024, 6, 1))
        self.assertEqual(cash["gross"], 10.0)
        self.assertEqual(stock["shares_after"], 200)

    def test_golden_300024_2010_mixed_distribution_preserves_equity(self):
        """300024 2010-04-16：10派1、10送2、10转10（双源已确认）。"""
        acct = VirtualAccount(0)
        pos = Position(shares=100, avg_cost=10.0, lots=[(100, date(2009, 1, 4))])
        acct.positions["300024"] = pos

        cash = acct.credit_dividend("300024", 0.1, date(2010, 4, 16))
        stock = acct.adjust_for_stock_dividend("300024", 1.2, date(2010, 4, 16))

        self.assertEqual(cash["gross"], 10.0)
        self.assertEqual(stock["shares_after"], 220)
        self.assertAlmostEqual(pos.avg_cost, 10.0 / 2.2)
        # 除权后 raw 价=(10-0.1)/(1+1.2)=4.5；现金10+220×4.5恰好等于除权前100×10。
        self.assertAlmostEqual(acct.equity({"300024": 4.5}), 1_000.0)


class TestCorporateActionCanonicalization(unittest.TestCase):
    def test_exact_source_duplicate_is_folded(self):
        event = DividendEventDTO(date(2024, 6, 1), 0.1, 1.0, date(2024, 5, 31))
        self.assertEqual(_canonical_events([event, event]), [event])

    def test_conflicting_same_ex_date_is_rejected(self):
        with self.assertRaises(CorporateActionConflictError):
            _canonical_events([
                DividendEventDTO(date(2024, 6, 1), 0.1, 0.0),
                DividendEventDTO(date(2024, 6, 1), 0.2, 0.0),
            ])

    def test_backtest_read_path_folds_exact_db_duplicates(self):
        rows = [
            DividendEvent(asset_code="002024", ex_date=date(2024, 6, 1), per_share_cash=0.1,
                          per_share_stock=1.0, source="source-a"),
            DividendEvent(asset_code="002024", ex_date=date(2024, 6, 1), per_share_cash=0.1,
                          per_share_stock=1.0, source="source-b"),
        ]
        canonical = _canonical_dividend_rows(rows)
        self.assertEqual(len(canonical), 1)
        self.assertEqual(canonical[0][1].per_share_stock, 1.0)

    def test_backtest_read_path_rejects_conflicting_db_duplicates(self):
        rows = [
            DividendEvent(asset_code="002024", ex_date=date(2024, 6, 1), per_share_cash=0.1,
                          source="source-a"),
            DividendEvent(asset_code="002024", ex_date=date(2024, 6, 1), per_share_cash=0.2,
                          source="source-b"),
        ]
        with self.assertRaises(CorporateActionConflictError):
            _canonical_dividend_rows(rows)

    def test_audit_marks_duplicate_and_empty_year_as_not_formal_ready(self):
        rows = [
            DividendEvent(asset_code="002024", ex_date=date(2007, 6, 1), per_share_cash=0.1,
                          source="source-a"),
            DividendEvent(asset_code="002024", ex_date=date(2007, 6, 1), per_share_cash=0.1,
                          source="source-b"),
        ]
        report = _summarize_corporate_action_rows(rows, start_year=2007, end_year=2008)
        self.assertFalse(report["ready_for_formal_backtest"])
        self.assertEqual(report["duplicate_groups"][0]["count"], 2)
        self.assertEqual(report["zero_event_years"], ["2008"])


class TestSettleDividendsGate(unittest.TestCase):
    """送转门控(2026-07-28 修复):credit_dividends 门控现金+送转结算。

    qfq/hfq 三复权价已含分红再投+送转,credit_dividends=False 时两者全跳过
    (再手动入账/调仓=重复计息)。此 bug 曾在聚宽交叉验证通过时于 benign 票池
    (0 送转事件)漏网——「通过≠无bug」(见 memory/backtest-crossvalidation-verdict),
    故补 hermetic 护栏防静默回归。002594 比亚迪 2025-07-29 10送20 已真跑验证。
    """

    def _acct(self, shares=400):
        acct = VirtualAccount(0)
        acct.positions["002594"] = Position(
            shares=shares, avg_cost=300.0, lots=[(shares, date(2025, 7, 1))])
        return acct

    def _divs(self):
        ex = date(2025, 7, 29)
        cash = {ex: [("002594", 3.974, date(2025, 7, 28))]}   # (code, 每股现金, 登记日)
        stock = {ex: [("002594", 2.0)]}                        # 10送20 → factor 3.0
        return ex, cash, stock

    def test_qfq_skips_both_cash_and_stock(self):
        acct = self._acct()
        ex, cash, stock = self._divs()
        recs = settle_dividends(acct, ex, cash, stock, credit_dividends=False)
        self.assertEqual(recs, [])
        self.assertEqual(acct.positions["002594"].shares, 400)   # 送转不调仓
        self.assertEqual(acct.cash, 0)                            # 现金不入账

    def test_raw_credits_cash_then_adjusts_stock_in_order(self):
        acct = self._acct()
        ex, cash, stock = self._divs()
        recs = settle_dividends(acct, ex, cash, stock, credit_dividends=True)
        self.assertEqual([r["kind"] for r in recs],
                         ["cash_dividend", "stock_dividend"])     # 现金先、送转后
        self.assertEqual(acct.positions["002594"].shares, 1200)   # 400 × factor(3.0)
        self.assertAlmostEqual(acct.cash, 1271.68, places=2)      # 1589.6 毛扣 20% 税

    def test_no_event_day_is_noop_under_raw(self):
        acct = self._acct()
        # 该日无事件 → 即使 raw 也不产生记录、不动仓位
        recs = settle_dividends(acct, date(2025, 7, 15), {}, {}, credit_dividends=True)
        self.assertEqual(recs, [])
        self.assertEqual(acct.positions["002594"].shares, 400)
        self.assertEqual(acct.cash, 0)


if __name__ == "__main__":
    unittest.main()
