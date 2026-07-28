from datetime import date

import pytest
from sqlmodel import SQLModel, Session, create_engine, select

from stockfu.data.base import DividendEventDTO
from stockfu.models import Asset, BackfillCheckpoint, DividendEvent
from stockfu.services.dividend import (
    CorporateActionConflictError,
    _KNOWN_DIVIDEND_RESOLUTIONS,
    _canonical_events,
    _repair_known_dividend_conflicts_in_session,
)


def _event(cash: float, announce: date, after_tax: float) -> DividendEventDTO:
    return DividendEventDTO(
        ex_date=date(2013, 6, 14), per_share_cash=cash,
        record_date=date(2013, 6, 13), announce_date=announce,
        pay_date=date(2013, 6, 14), per_share_cash_after_tax=after_tax,
        source="baostock:dividend/2013",
    )


def test_known_resolution_returns_audited_aggregate():
    result = _canonical_events([
        _event(0.021, date(2013, 3, 30), 0.0189),
        _event(0.041, date(2013, 5, 17), 0.0369),
    ], code="000738")

    assert len(result) == 1
    assert result[0].per_share_cash == pytest.approx(0.062)
    assert result[0].per_share_cash_after_tax == pytest.approx(0.0558)
    assert result[0].announce_date == date(2013, 5, 17)


def test_known_resolution_rejects_changed_source_candidates():
    with pytest.raises(CorporateActionConflictError, match="来源候选发生变化"):
        _canonical_events([
            _event(0.021, date(2013, 3, 30), 0.0189),
            _event(0.041, date(2013, 5, 17), 0.0369),
            _event(0.001, date(2013, 5, 17), 0.0009),
        ], code="000738")


def test_known_resolution_accepts_audited_source_subset():
    result = _canonical_events([
        _event(0.021, date(2013, 3, 30), 0.0189),
    ], code="000738")
    assert result[0].per_share_cash == pytest.approx(0.062)


@pytest.mark.parametrize(
    ("key", "cash", "after_tax"),
    [
        (("300315", date(2012, 10, 22)), .15, .135),
        (("300760", date(2026, 5, 28)), 1.56, 1.404),
        (("600989", date(2021, 5, 20)), .58563, .52707),
    ],
)
def test_newly_audited_resolution_aggregates_source_candidates(key, cash, after_tax):
    resolution = _KNOWN_DIVIDEND_RESOLUTIONS[key]
    result = _canonical_events(list(resolution.expected), code=key[0])

    assert result == [resolution.final]
    assert result[0].per_share_cash == pytest.approx(cash)
    assert result[0].per_share_cash_after_tax == pytest.approx(after_tax)


def test_repair_is_transactional_and_idempotent():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Asset(code="002434"))
        session.add(Asset(code="002601"))
        session.add(BackfillCheckpoint(
            task_key="dividend", scope_key="v1:2007-2026", item_key="300315",
            status="success", attempts=1,
        ))
        session.add_all([
            DividendEvent(
                asset_code="002601", ex_date=date(2018, 10, 25),
                record_date=date(2018, 10, 24), announce_date=date(2018, 8, 21),
                per_share_cash=0.65, per_share_stock=0.0,
            ),
            DividendEvent(
                asset_code="002601", ex_date=date(2018, 10, 25),
                record_date=date(2018, 10, 24), announce_date=date(2018, 8, 21),
                per_share_cash=0.654028, per_share_stock=0.0,
            ),
        ])
        session.commit()

        resolutions = {
            key: _KNOWN_DIVIDEND_RESOLUTIONS[key]
            for key in (("002434", date(2018, 5, 22)), ("002601", date(2018, 10, 25)))
        }
        summary = _repair_known_dividend_conflicts_in_session(session, resolutions)
        session.commit()
        assert summary["inserted"] == 1
        assert summary["deleted"] == 1
        assert summary["unresolved_reset"] == 0

        rows = list(session.exec(select(DividendEvent).where(
            DividendEvent.asset_code == "002601",
            DividendEvent.ex_date == date(2018, 10, 25),
        )))
        assert len(rows) == 1
        assert rows[0].per_share_cash == pytest.approx(0.65)
        assert rows[0].per_share_cash_after_tax == pytest.approx(0.585)
        checkpoint = session.exec(select(BackfillCheckpoint).where(
            BackfillCheckpoint.item_key == "300315",
        )).one()
        assert checkpoint.status == "success"

        again = _repair_known_dividend_conflicts_in_session(session, resolutions)
        session.commit()
        assert again["inserted"] == again["updated"] == again["deleted"] == 0
        assert again["unresolved_reset"] == 0
