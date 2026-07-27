"""严格账户的公司行为结算金标。"""
from contextlib import contextmanager
from datetime import date

import pytest
from sqlmodel import SQLModel, Session, create_engine

from stockfu.backtest import engine as backtest_engine
from stockfu.backtest.engine import (
    CorporateActionCoverageError, Position, VirtualAccount,
    _preload_accepted_corporate_actions,
)
from stockfu.models import Asset, CorporateActionEvent, DividendEvent


def test_cash_dividend_is_receivable_until_payment_date():
    acct = VirtualAccount(100)
    acct.positions["A"] = Position(shares=100, avg_cost=10.0,
                                   lots=[(100, date(2024, 1, 1))])

    accrued = acct.accrue_cash_dividend("A", 1.0, date(2024, 6, 1))
    assert accrued["gross"] == 100.0
    assert acct.cash == 100.0
    assert acct.cash_receivable == 100.0
    assert acct.equity({"A": 10.0}) == 1_200.0

    settled = acct.settle_cash_dividend("A", 100.0, date(2024, 6, 20))
    assert settled["net"] == 100.0
    assert acct.cash == 200.0
    assert acct.cash_receivable == 0.0


def test_stock_dividend_is_not_sellable_before_listing_date():
    acct = VirtualAccount(1_000)
    acct.positions["A"] = Position(shares=100, avg_cost=10.0,
                                   lots=[(100, date(2024, 1, 1))])

    accrued = acct.accrue_stock_dividend("A", 1.0, date(2024, 6, 1))
    assert accrued["shares"] == 100
    assert acct.positions["A"].shares == 100
    assert acct.positions["A"].receivable_shares == 100
    # 可卖仓位仍是 100；即使目标为零，成交也不能卖出尚未上市的 100 股应收。
    rec = acct.apply_action("A", "sell", 0.0, 10.0, {"A": 10.0}, date(2024, 6, 2))
    assert rec["shares"] == -100
    assert acct.positions["A"].receivable_shares == 100


def test_delisting_uses_explicit_terminal_settlement_not_last_quote():
    acct = VirtualAccount(100)
    acct.positions["A"] = Position(shares=100, receivable_shares=20, avg_cost=10.0)

    rec = acct.settle_delisting("A", 3.5, date(2024, 6, 30))
    assert rec["proceeds"] == 420.0
    assert acct.cash == 520.0
    assert "A" not in acct.positions


def test_rights_exercise_freezes_cash_and_lists_as_new_cost_lot():
    acct = VirtualAccount(300)
    acct.positions["A"] = Position(shares=100, avg_cost=10.0,
                                   lots=[(100, date(2024, 1, 1))])

    rec = acct.exercise_rights("A", 0.15, 14.43, "exercise_if_cash_available", date(2024, 1, 27))
    assert rec["shares"] == 15
    assert rec["cost"] == 216.45
    assert acct.cash == pytest.approx(83.55)
    assert acct.positions["A"].receivable_shares == 15

    settled = acct.settle_rights("A", 15, 216.45, date(2024, 2, 15))
    assert settled["shares_after"] == 115
    assert acct.positions["A"].avg_cost == pytest.approx((1000 + 216.45) / 115)
    assert acct.positions["A"].lots[-1] == (15, date(2024, 2, 15))


def test_rights_ignore_does_not_invent_a_right_value():
    acct = VirtualAccount(300)
    acct.positions["A"] = Position(shares=100, avg_cost=10.0)
    rec = acct.exercise_rights("A", 0.15, 14.43, "ignore", date(2024, 1, 27))
    assert rec["kind"] == "rights_ignored"
    assert acct.cash == 300
    assert acct.positions["A"].receivable_shares == 0


def test_strict_loader_rejects_legacy_only_or_unreviewed_event(monkeypatch):
    db = create_engine("sqlite://")
    SQLModel.metadata.create_all(db)

    @contextmanager
    def temp_session_scope():
        with Session(db) as session:
            yield session

    monkeypatch.setattr(backtest_engine, "session_scope", temp_session_scope)
    with Session(db) as session:
        session.add(Asset(code="600001"))
        session.add(DividendEvent(asset_code="600001", ex_date=date(2024, 6, 1),
                                  per_share_cash=1.0))
        session.commit()
    with pytest.raises(CorporateActionCoverageError, match="旧 dividend_event"):
        _preload_accepted_corporate_actions(["600001"], date(2024, 1, 1), date(2024, 12, 31))

    with Session(db) as session:
        session.add(CorporateActionEvent(
            action_id="600001:2024-06-01:distribution", asset_code="600001",
            ex_date=date(2024, 6, 1), per_share_cash=1.0, status="needs_review",
        ))
        session.commit()
    with pytest.raises(CorporateActionCoverageError, match="needs_review"):
        _preload_accepted_corporate_actions(["600001"], date(2024, 1, 1), date(2024, 12, 31))


def test_strict_loader_requires_settlement_dates(monkeypatch):
    db = create_engine("sqlite://")
    SQLModel.metadata.create_all(db)

    @contextmanager
    def temp_session_scope():
        with Session(db) as session:
            yield session

    monkeypatch.setattr(backtest_engine, "session_scope", temp_session_scope)
    with Session(db) as session:
        session.add(Asset(code="600001"))
        session.add(CorporateActionEvent(
            action_id="600001:2024-06-01:distribution", asset_code="600001",
            ex_date=date(2024, 6, 1), per_share_cash=1.0, status="accepted",
        ))
        session.commit()

    with pytest.raises(CorporateActionCoverageError, match="缺少支付日"):
        _preload_accepted_corporate_actions(["600001"], date(2024, 1, 1), date(2024, 12, 31))


def test_strict_loader_requires_verified_delisting_terminal_price(monkeypatch):
    db = create_engine("sqlite://")
    SQLModel.metadata.create_all(db)

    @contextmanager
    def temp_session_scope():
        with Session(db) as session:
            yield session

    monkeypatch.setattr(backtest_engine, "session_scope", temp_session_scope)
    with Session(db) as session:
        session.add(Asset(code="600001"))
        session.add(CorporateActionEvent(
            action_id="600001:2024-06-01:delisting", asset_code="600001",
            action_type="delisting", ex_date=date(2024, 6, 1), status="accepted",
        ))
        session.commit()
    with pytest.raises(CorporateActionCoverageError, match="终止结算价"):
        _preload_accepted_corporate_actions(["600001"], date(2024, 1, 1), date(2024, 12, 31))


def test_strict_loader_requires_explicit_rights_policy(monkeypatch):
    db = create_engine("sqlite://")
    SQLModel.metadata.create_all(db)

    @contextmanager
    def temp_session_scope():
        with Session(db) as session:
            yield session

    monkeypatch.setattr(backtest_engine, "session_scope", temp_session_scope)
    with Session(db) as session:
        session.add(Asset(code="600001"))
        session.add(CorporateActionEvent(
            action_id="600001:2024-01-27:rights", asset_code="600001", action_type="rights",
            ex_date=date(2024, 1, 27), rights_ratio=0.15, rights_price=14.43,
            stock_mkt_date=date(2024, 2, 15), status="accepted",
        ))
        session.commit()
    with pytest.raises(CorporateActionCoverageError, match="行权策略"):
        _preload_accepted_corporate_actions(["600001"], date(2024, 1, 1), date(2024, 12, 31))
    by_ex, _ = _preload_accepted_corporate_actions(
        ["600001"], date(2024, 1, 1), date(2024, 12, 31),
        rights_policy="exercise_if_cash_available")
    assert by_ex[date(2024, 1, 27)][0].action_type == "rights"
