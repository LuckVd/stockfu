from datetime import date
from contextlib import contextmanager

import pytest
from sqlmodel import SQLModel, Session, create_engine, select

from stockfu.models import CorporateActionEvent, CorporateActionSourceRecord
from stockfu.data.base import DelistingEventDTO, DividendEventDTO, DividendMetric, RightsIssueDTO
from stockfu.services import corporate_actions
from stockfu.services.corporate_actions import (
    CorporateActionCandidate, action_id_for, propose_arbitration, source_event_key,
    evidence_tier_for_source, source_provider_key, stage_dividend_metric,
    candidate_from_source_record, stage_legacy_dividend_events,
    stage_delisting_events,
    stage_rights_issue_events,
)
from stockfu.models import DividendEvent


def _candidate(source_id: int, **overrides) -> CorporateActionCandidate:
    values = {
        "source_record_id": source_id, "asset_code": "600519", "action_type": "distribution",
        "ex_date": date(2018, 6, 15), "per_share_cash": 1.45,
    }
    values.update(overrides)
    return CorporateActionCandidate(**values)


def test_matching_sources_propose_one_accepted_event():
    proposal = propose_arbitration([
        _candidate(2, source="source-b"), _candidate(1, source="source-a"),
    ])[0]

    assert proposal.action_id == "600519:2018-06-15:distribution"
    assert proposal.status == "accepted"
    assert proposal.source_record_ids == (1, 2)
    assert proposal.decision_note == "matching_sources"


def test_conflicting_sources_never_auto_accept():
    proposal = propose_arbitration([
        _candidate(1, source="source-a"),
        _candidate(2, source="source-b", per_share_cash=1.46),
    ])[0]

    assert proposal.status == "needs_review"
    assert proposal.decision_note == "conflicting_source_terms"


def test_missing_date_is_filled_from_another_matching_source():
    proposal = propose_arbitration([
        _candidate(1, source="source-a"),
        _candidate(2, source="source-b", record_date=date(2018, 6, 14)),
    ])[0]

    assert proposal.status == "accepted"
    assert proposal.candidate.record_date == date(2018, 6, 14)


def test_two_different_known_dates_require_review():
    proposal = propose_arbitration([
        _candidate(1, source="source-a", record_date=date(2018, 6, 14)),
        _candidate(2, source="source-b", record_date=date(2018, 6, 13)),
    ])[0]

    assert proposal.status == "needs_review"


def test_different_announcement_semantics_do_not_block_matching_settlement_terms():
    proposal = propose_arbitration([
        _candidate(1, source="source-a", announce_date=date(2018, 4, 1)),
        _candidate(2, source="source-b", announce_date=date(2018, 6, 1)),
    ])[0]

    assert proposal.status == "accepted"
    assert proposal.decision_note == "matching_sources_announcement_variance"


def test_action_id_includes_action_type_to_avoid_cross_type_merge():
    distribution = _candidate(1, source="source-a")
    rights = _candidate(2, source="source-a", action_type="rights", rights_ratio=0.2, rights_price=8.0)

    assert action_id_for(distribution) != action_id_for(rights)
    assert len(propose_arbitration([distribution, rights])) == 2


def test_negative_economic_terms_are_rejected():
    with pytest.raises(ValueError, match="不能为负"):
        propose_arbitration([_candidate(1, per_share_stock=-0.1)])


def test_matching_single_source_requires_review():
    proposal = propose_arbitration([_candidate(1, source="source-a")])[0]
    assert proposal.status == "needs_review"
    assert proposal.decision_note == "single_source"


def test_source_detail_is_not_mistaken_for_a_second_independent_provider():
    proposal = propose_arbitration([
        _candidate(1, source="baostock:dividend/2011"),
        _candidate(2, source="baostock:dividend/2012"),
    ])[0]
    assert source_provider_key("baostock:dividend/2011") == "baostock"
    assert proposal.status == "needs_review"


def test_formal_account_event_source_overrides_same_provider_legacy_metric():
    proposal = propose_arbitration([
        _candidate(1, source="baostock:dividend/2010", per_share_stock=1.2),
        _candidate(2, source="akshare:stock_fhps_detail_em", per_share_stock=0.0,
                   per_share_cash=0.045455, evidence_tier=0),
        _candidate(3, source="akshare:stock_history_dividend_detail:pre_event_share",
                   per_share_stock=1.2, evidence_tier=1),
    ])[0]
    assert evidence_tier_for_source("akshare:stock_fhps_detail_em") == 0
    assert evidence_tier_for_source("akshare:stock_history_dividend_detail:pre_event_share") == 1
    assert proposal.status == "accepted"
    assert proposal.source_record_ids == (1, 3)


def test_generated_source_key_is_stable_but_changes_for_a_revision():
    candidate = _candidate(1)
    assert source_event_key("baostock", candidate) == source_event_key("baostock", candidate)
    assert source_event_key("baostock", candidate) != source_event_key(
        "baostock", _candidate(1, per_share_cash=1.46)
    )


def test_staging_and_materialization_are_append_only_and_idempotent(monkeypatch):
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)

    @contextmanager
    def temp_session_scope():
        with Session(engine) as session:
            yield session

    monkeypatch.setattr(corporate_actions, "session_scope", temp_session_scope)
    first = corporate_actions.stage_source_records("source-a", [_candidate(None)])
    second = corporate_actions.stage_source_records("source-a", [_candidate(None)])
    assert first == {"added": 1, "skipped": 0}
    assert second == {"added": 0, "skipped": 1}

    first_run = corporate_actions.materialize_arbitration_proposals()
    second_run = corporate_actions.materialize_arbitration_proposals()
    assert first_run == {"added": 1, "skipped": 0, "accepted": 0, "needs_review": 1}
    assert second_run == {"added": 0, "skipped": 1, "accepted": 0, "needs_review": 0}
    with Session(engine) as session:
        assert len(session.exec(select(CorporateActionSourceRecord)).all()) == 1
        event = session.exec(select(CorporateActionEvent)).one()
        assert event.revision == 1
        assert event.status == "needs_review"


def test_source_revision_creates_a_new_event_revision_for_review(monkeypatch):
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)

    @contextmanager
    def temp_session_scope():
        with Session(engine) as session:
            yield session

    monkeypatch.setattr(corporate_actions, "session_scope", temp_session_scope)
    corporate_actions.stage_source_records("source-a", [_candidate(None)])
    corporate_actions.materialize_arbitration_proposals()
    corporate_actions.stage_source_records("source-a", [_candidate(None, per_share_cash=1.46)])
    report = corporate_actions.materialize_arbitration_proposals()

    assert report == {"added": 1, "skipped": 0, "accepted": 0, "needs_review": 1}
    with Session(engine) as session:
        events = session.exec(select(CorporateActionEvent).order_by(
            CorporateActionEvent.revision
        )).all()
        assert [event.status for event in events] == ["needs_review", "needs_review"]
        assert events[1].supersedes_event_id == events[0].id


def test_dividend_metric_is_staged_without_touching_legacy_event_table(monkeypatch):
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)

    @contextmanager
    def temp_session_scope():
        with Session(engine) as session:
            yield session

    monkeypatch.setattr(corporate_actions, "session_scope", temp_session_scope)
    metric = DividendMetric(code="600519", events=[DividendEventDTO(
        ex_date=date(2018, 6, 15), per_share_cash=1.45, source="baostock:dividend/2017",
    )])
    assert stage_dividend_metric("600519", metric) == {"sources": 1, "added": 1, "skipped": 0}
    with Session(engine) as session:
        row = session.exec(select(CorporateActionSourceRecord)).one()
        assert row.asset_code == "600519"
        assert row.per_share_cash == 1.45


def test_dividend_metric_preserves_baostock_settlement_dates(monkeypatch):
    """BaoStock 的支付日和送转上市日必须进入不可变来源证据。"""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)

    @contextmanager
    def temp_session_scope():
        with Session(engine) as session:
            yield session

    monkeypatch.setattr(corporate_actions, "session_scope", temp_session_scope)
    metric = DividendMetric(code="300024", events=[DividendEventDTO(
        ex_date=date(2010, 4, 16), per_share_cash=0.1, per_share_stock=1.2,
        pay_date=date(2010, 4, 22), stock_mkt_date=date(2010, 4, 23),
        source="baostock:dividend/2009",
    )])
    assert stage_dividend_metric("300024", metric) == {"sources": 1, "added": 1, "skipped": 0}
    with Session(engine) as session:
        row = session.exec(select(CorporateActionSourceRecord)).one()
        assert row.pay_date == date(2010, 4, 22)
        assert row.stock_mkt_date == date(2010, 4, 23)


def test_legacy_dividend_rows_become_source_evidence_not_formal_events(monkeypatch):
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)

    @contextmanager
    def temp_session_scope():
        with Session(engine) as session:
            yield session

    monkeypatch.setattr(corporate_actions, "session_scope", temp_session_scope)
    with Session(engine) as session:
        session.add(DividendEvent(asset_code="600519", ex_date=date(2018, 6, 15),
                                  per_share_cash=1.45, source="akshare:legacy"))
        session.commit()
    report = stage_legacy_dividend_events(["600519"], start=date(2007, 1, 1), end=date(2026, 1, 1))
    assert report == {"sources": 1, "added": 1, "skipped": 0}
    with Session(engine) as session:
        assert len(session.exec(select(CorporateActionSourceRecord)).all()) == 1
        assert len(session.exec(select(CorporateActionEvent)).all()) == 0


def test_rights_issue_is_staged_as_single_source_evidence(monkeypatch):
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)

    @contextmanager
    def temp_session_scope():
        with Session(engine) as session:
            yield session

    monkeypatch.setattr(corporate_actions, "session_scope", temp_session_scope)
    event = RightsIssueDTO(code="600030", ex_date=date(2022, 1, 27),
                           rights_ratio=0.15, rights_price=14.43,
                           stock_mkt_date=date(2022, 2, 15),
                           source="akshare:stock_allotment_cninfo")
    assert stage_rights_issue_events([event]) == {"sources": 1, "added": 1, "skipped": 0}
    with Session(engine) as session:
        row = session.exec(select(CorporateActionSourceRecord)).one()
        assert (row.action_type, row.rights_ratio, row.rights_price) == ("rights", 0.15, 14.43)
        proposal = propose_arbitration([candidate_from_source_record(row)])[0]
        assert proposal.status == "needs_review"


def test_delisting_event_without_settlement_is_single_source_evidence(monkeypatch):
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)

    @contextmanager
    def temp_session_scope():
        with Session(engine) as session:
            yield session

    monkeypatch.setattr(corporate_actions, "session_scope", temp_session_scope)
    event = DelistingEventDTO(code="000024", event_date=date(2015, 12, 30),
                               action_type="delisting", source="szse:stock_info_sz_delist")
    assert stage_delisting_events([event]) == {"sources": 1, "added": 1, "skipped": 0}
    with Session(engine) as session:
        row = session.exec(select(CorporateActionSourceRecord)).one()
        assert (row.action_type, row.ex_date) == ("delisting", date(2015, 12, 30))
        proposal = propose_arbitration([candidate_from_source_record(row)])[0]
        assert proposal.status == "needs_review"
