"""公司行为的来源归并与仲裁前校验。

本模块刻意不写 ``dividend_event``：旧表仍是探索性功能的兼容读模型；正式回测
只能在来源记录被仲裁为 accepted 之后读取 ``corporate_action_event``。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
import hashlib
import json
from typing import Iterable

from sqlmodel import select

from stockfu.db import session_scope
from stockfu.data.base import DividendMetric
from stockfu.data.base import DelistingEventDTO
from stockfu.data.base import RightsIssueDTO
from stockfu.models import Asset, CorporateActionEvent, CorporateActionSourceRecord
from stockfu.models import DividendEvent


@dataclass(frozen=True)
class CorporateActionCandidate:
    """不依赖数据库的来源记录视图，便于测试和多个抓取器复用。"""
    source_record_id: int | None
    asset_code: str
    action_type: str
    ex_date: date
    source: str = ""
    evidence_tier: int = 1                     # 0=legacy display metric, 1=account-event source
    per_share_cash: float = 0.0
    per_share_stock: float = 0.0
    rights_ratio: float = 0.0
    rights_price: float | None = None
    terminal_price: float | None = None
    record_date: date | None = None
    announce_date: date | None = None
    pay_date: date | None = None
    stock_mkt_date: date | None = None
    currency: str = "CNY"


@dataclass(frozen=True)
class ArbitrationProposal:
    """一个逻辑事件的仲裁结果；冲突不会被自动 accepted。"""
    action_id: str
    status: str
    candidate: CorporateActionCandidate
    source_record_ids: tuple[int, ...]
    decision_note: str


def action_id_for(candidate: CorporateActionCandidate) -> str:
    return f"{candidate.asset_code}:{candidate.ex_date.isoformat()}:{candidate.action_type}"


def source_provider_key(source: str) -> str:
    """从带明细的来源标签提取独立提供方身份。

    例如 ``baostock:dividend/2011`` 与 ``baostock:dividend/2012`` 都只能算一个
    来源；只有 BaoStock 与 AkShare/交易所公告等不同提供方的相同证据才能自动接受。
    """
    return source.split(":", 1)[0].strip().lower()


def evidence_tier_for_source(source: str) -> int:
    """区分历史展示口径与可用于账户结算的来源记录。"""
    if source.endswith(":pre_event_share"):
        return 1
    return 0 if source.startswith("akshare:") else 1


def _economic_signature(candidate: CorporateActionCandidate) -> tuple:
    """仅比较经济条款；来源不同但条款一致可共同支持一条正式事件。"""
    return (
        candidate.per_share_cash, candidate.per_share_stock,
        candidate.rights_ratio, candidate.rights_price, candidate.terminal_price,
        candidate.currency,
    )


def _dates_compatible(left: CorporateActionCandidate, right: CorporateActionCandidate) -> bool:
    """结算相关日期缺失可补齐；两个已知且不同的值才是硬冲突。"""
    return all(
        a is None or b is None or a == b
        for a, b in (
            (left.record_date, right.record_date),
            (left.pay_date, right.pay_date),
            (left.stock_mkt_date, right.stock_mkt_date),
        )
    )


def _has_announcement_variance(candidates: list[CorporateActionCandidate]) -> bool:
    """预案公告与实施公告常被不同来源映射到同字段，保留差异但不阻断结算。"""
    known = {candidate.announce_date for candidate in candidates if candidate.announce_date}
    return len(known) > 1


def _merge_known_dates(candidates: list[CorporateActionCandidate]) -> CorporateActionCandidate:
    """在已确认兼容的记录中，保留任一来源给出的日期证据。"""
    representative = candidates[0]

    def first_known(name: str):
        return next((getattr(candidate, name) for candidate in candidates
                     if getattr(candidate, name) is not None), None)

    return replace(
        representative,
        record_date=first_known("record_date"),
        announce_date=first_known("announce_date"),
        pay_date=first_known("pay_date"),
        stock_mkt_date=first_known("stock_mkt_date"),
        terminal_price=first_known("terminal_price"),
    )


def _effective_candidates(candidates: list[CorporateActionCandidate]) -> list[CorporateActionCandidate]:
    """每个提供方只采用最高证据等级，低等级记录保留但不污染正式仲裁。"""
    by_provider: dict[str, list[CorporateActionCandidate]] = {}
    for candidate in candidates:
        by_provider.setdefault(source_provider_key(candidate.source), []).append(candidate)
    effective: list[CorporateActionCandidate] = []
    for provider_records in by_provider.values():
        highest = max(record.evidence_tier for record in provider_records)
        effective.extend(record for record in provider_records if record.evidence_tier == highest)
    return effective


def propose_arbitration(records: Iterable[CorporateActionCandidate]) -> list[ArbitrationProposal]:
    """按逻辑事件归并来源记录。

    至少两个不同来源的条款一致才自动提出 ``accepted``；单一来源或任何经济条款、
    关键日期不一致都提出 ``needs_review``，禁止以抓取顺序决定结果。
    """
    groups: dict[str, list[CorporateActionCandidate]] = {}
    for record in records:
        if record.ex_date is None:
            raise ValueError("公司行为来源记录必须有 ex_date")
        if record.per_share_cash < 0 or record.per_share_stock < 0 or record.rights_ratio < 0:
            raise ValueError(f"{record.asset_code} {record.ex_date}: 公司行为比率不能为负")
        groups.setdefault(action_id_for(record), []).append(record)
    proposals: list[ArbitrationProposal] = []
    for action_id, candidates in sorted(groups.items()):
        candidates = _effective_candidates(candidates)
        representative = _merge_known_dates(candidates)
        ids = tuple(sorted(record.source_record_id for record in candidates
                           if record.source_record_id is not None))
        consistent = all(_economic_signature(record) == _economic_signature(representative)
                         and _dates_compatible(record, representative)
                         for record in candidates[1:])
        sources = {source_provider_key(record.source) for record in candidates if record.source}
        if consistent and len(sources) >= 2:
            note = ("matching_sources_announcement_variance"
                    if _has_announcement_variance(candidates) else "matching_sources")
            proposals.append(ArbitrationProposal(
                action_id=action_id, status="accepted", candidate=representative,
                source_record_ids=ids, decision_note=note,
            ))
        elif consistent:
            proposals.append(ArbitrationProposal(
                action_id=action_id, status="needs_review", candidate=representative,
                source_record_ids=ids, decision_note="single_source",
            ))
        else:
            proposals.append(ArbitrationProposal(
                action_id=action_id, status="needs_review", candidate=representative,
                source_record_ids=ids, decision_note="conflicting_source_terms",
            ))
    return proposals


def source_event_key(source: str, candidate: CorporateActionCandidate) -> str:
    """没有源端稳定ID时，为一条不可变观测生成确定性键。"""
    payload = {
        "source": source,
        "asset_code": candidate.asset_code,
        "action_type": candidate.action_type,
        "ex_date": candidate.ex_date.isoformat(),
        "cash": candidate.per_share_cash,
        "stock": candidate.per_share_stock,
        "rights_ratio": candidate.rights_ratio,
        "rights_price": candidate.rights_price,
        "terminal_price": candidate.terminal_price,
        "record_date": candidate.record_date.isoformat() if candidate.record_date else None,
        "announce_date": candidate.announce_date.isoformat() if candidate.announce_date else None,
        "pay_date": candidate.pay_date.isoformat() if candidate.pay_date else None,
        "stock_mkt_date": candidate.stock_mkt_date.isoformat() if candidate.stock_mkt_date else None,
        "currency": candidate.currency,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def candidate_from_source_record(row: CorporateActionSourceRecord) -> CorporateActionCandidate:
    return CorporateActionCandidate(
        source_record_id=row.id,
        source=row.source,
        evidence_tier=evidence_tier_for_source(row.source),
        asset_code=row.asset_code,
        action_type=row.action_type,
        ex_date=row.ex_date,
        per_share_cash=row.per_share_cash,
        per_share_stock=row.per_share_stock,
        rights_ratio=row.rights_ratio,
        rights_price=row.rights_price,
        terminal_price=row.terminal_price,
        record_date=row.record_date,
        announce_date=row.announce_date,
        pay_date=row.pay_date,
        stock_mkt_date=row.stock_mkt_date,
        currency=row.currency,
    )


def stage_source_records(source: str, candidates: Iterable[CorporateActionCandidate]) -> dict:
    """幂等追加来源观测；同键不改写，修订条款会自然成为新证据行。"""
    added = skipped = 0
    with session_scope() as session:
        for candidate in candidates:
            key = source_event_key(source, candidate)
            existing = session.exec(select(CorporateActionSourceRecord).where(
                CorporateActionSourceRecord.source == source,
                CorporateActionSourceRecord.source_event_key == key,
            )).first()
            if existing:
                skipped += 1
                continue
            payload = json.dumps({
                "asset_code": candidate.asset_code, "action_type": candidate.action_type,
                "ex_date": candidate.ex_date.isoformat(), "per_share_cash": candidate.per_share_cash,
                "per_share_stock": candidate.per_share_stock, "rights_ratio": candidate.rights_ratio,
                "rights_price": candidate.rights_price,
                "terminal_price": candidate.terminal_price,
            }, sort_keys=True, separators=(",", ":"))
            session.add(CorporateActionSourceRecord(
                asset_code=candidate.asset_code, source=source, source_event_key=key,
                action_type=candidate.action_type, ex_date=candidate.ex_date,
                record_date=candidate.record_date, announce_date=candidate.announce_date,
                pay_date=candidate.pay_date, stock_mkt_date=candidate.stock_mkt_date,
                per_share_cash=candidate.per_share_cash, per_share_stock=candidate.per_share_stock,
                rights_ratio=candidate.rights_ratio, rights_price=candidate.rights_price,
                terminal_price=candidate.terminal_price,
                currency=candidate.currency, payload_sha256=hashlib.sha256(payload.encode()).hexdigest(),
                raw_payload=payload,
            ))
            added += 1
        session.commit()
    return {"added": added, "skipped": skipped}


def stage_dividend_metric(code: str, metric: DividendMetric) -> dict:
    """把既有数据源适配器的结果暂存为证据，绝不写旧 ``dividend_event``。

    每条 DTO 的 ``source`` 会独立保存，以便后续把 BaoStock、交易所公告和其他
    来源并列仲裁；没有事件或缺少来源的输入一律拒绝，而非制造“覆盖完成”假象。
    """
    by_source: dict[str, list[CorporateActionCandidate]] = {}
    for event in metric.events:
        if not event.source:
            raise ValueError(f"{code} {event.ex_date}: 公司行为来源不能为空")
        by_source.setdefault(event.source, []).append(CorporateActionCandidate(
            source_record_id=None, source=event.source,
            evidence_tier=evidence_tier_for_source(event.source),
            asset_code=code, action_type="distribution",
            ex_date=event.ex_date, per_share_cash=float(event.per_share_cash or 0.0),
            per_share_stock=float(event.per_share_stock or 0.0),
            record_date=event.record_date, announce_date=event.announce_date,
            pay_date=event.pay_date, stock_mkt_date=event.stock_mkt_date,
            currency=event.currency or "CNY",
        ))
    added = skipped = 0
    for source, candidates in by_source.items():
        report = stage_source_records(source, candidates)
        added += report["added"]
        skipped += report["skipped"]
    return {"sources": len(by_source), "added": added, "skipped": skipped}


def stage_rights_issue_events(events: Iterable[RightsIssueDTO]) -> dict:
    """将配股实施方案作为独立来源证据暂存，不做单源自动放行。"""
    by_source: dict[str, list[CorporateActionCandidate]] = {}
    for event in events:
        if not event.source:
            raise ValueError(f"{event.code} {event.ex_date}: 配股来源不能为空")
        by_source.setdefault(event.source, []).append(CorporateActionCandidate(
            source_record_id=None, source=event.source, asset_code=event.code,
            action_type="rights", ex_date=event.ex_date, rights_ratio=event.rights_ratio,
            rights_price=event.rights_price, record_date=event.record_date,
            announce_date=event.announce_date, pay_date=event.pay_date,
            stock_mkt_date=event.stock_mkt_date, currency=event.currency or "CNY",
        ))
    added = skipped = 0
    for source, candidates in by_source.items():
        report = stage_source_records(source, candidates)
        added += report["added"]
        skipped += report["skipped"]
    return {"sources": len(by_source), "added": added, "skipped": skipped}


def stage_delisting_events(events: Iterable[DelistingEventDTO]) -> dict:
    """将交易所退市名单暂存为事件证据；结算价格缺失时绝不自动放行。"""
    events = list(events)
    # 来源表引用 asset；退市证券通常尚未被现有行情池创建。这里只建立最小身份记录，
    # 不把交易所名单的日期偷写为 security_master 的最终退市语义。
    with session_scope() as session:
        for event in events:
            if not session.get(Asset, event.code):
                session.add(Asset(code=event.code, name=event.name, market="cn"))
        session.commit()

    by_source: dict[str, list[CorporateActionCandidate]] = {}
    for event in events:
        if not event.source:
            raise ValueError(f"{event.code} {event.event_date}: 退市来源不能为空")
        by_source.setdefault(event.source, []).append(CorporateActionCandidate(
            source_record_id=None, source=event.source, asset_code=event.code,
            action_type=event.action_type, ex_date=event.event_date,
            announce_date=event.event_date, terminal_price=event.terminal_price,
        ))
    added = skipped = 0
    for source, candidates in by_source.items():
        report = stage_source_records(source, candidates)
        added += report["added"]
        skipped += report["skipped"]
    return {"sources": len(by_source), "added": added, "skipped": skipped}


def stage_legacy_dividend_events(codes: Iterable[str], *, start: date, end: date) -> dict:
    """把旧 ``dividend_event`` 作为带来源标签的历史证据导入新表。

    这不是把旧事件提升为正式事件：旧表只提供一条可追溯的证据来源，仍必须与
    新抓取或官方材料相互印证后才可能 accepted。
    """
    codes = list(codes)
    if not codes:
        return {"sources": 0, "added": 0, "skipped": 0}
    with session_scope() as session:
        rows = session.exec(select(DividendEvent).where(
            DividendEvent.asset_code.in_(codes),
            DividendEvent.ex_date >= start,
            DividendEvent.ex_date <= end,
        )).all()
    by_source: dict[str, list[CorporateActionCandidate]] = {}
    for row in rows:
        source = row.source or "legacy:dividend_event"
        by_source.setdefault(source, []).append(CorporateActionCandidate(
            source_record_id=None, source=source, asset_code=row.asset_code,
            action_type="distribution", ex_date=row.ex_date,
            per_share_cash=float(row.per_share_cash or 0.0),
            per_share_stock=float(row.per_share_stock or 0.0),
            record_date=row.record_date, announce_date=row.announce_date,
            currency=row.currency or "CNY",
        ))
    added = skipped = 0
    for source, candidates in by_source.items():
        report = stage_source_records(source, candidates)
        added += report["added"]
        skipped += report["skipped"]
    return {"sources": len(by_source), "added": added, "skipped": skipped}


def materialize_arbitration_proposals() -> dict:
    """将来源记录的仲裁建议 append-only 写为正式事件版本，返回统计。

    本函数不会写旧 ``dividend_event``，因此不会改变当前探索性回测；同一建议再次
    运行是幂等的，条款或来源集合变化才产生更高 revision。
    """
    added = skipped = accepted = needs_review = 0
    with session_scope() as session:
        records = session.exec(select(CorporateActionSourceRecord)).all()
        proposals = propose_arbitration(candidate_from_source_record(row) for row in records)
        for proposal in proposals:
            latest = session.exec(select(CorporateActionEvent).where(
                CorporateActionEvent.action_id == proposal.action_id,
            ).order_by(CorporateActionEvent.revision.desc())).first()
            source_ids = json.dumps(proposal.source_record_ids, separators=(",", ":"))
            candidate = proposal.candidate
            same = latest and (
                latest.status == proposal.status
                and latest.source_record_ids == source_ids
                and latest.per_share_cash == candidate.per_share_cash
                and latest.per_share_stock == candidate.per_share_stock
                and latest.rights_ratio == candidate.rights_ratio
                and latest.rights_price == candidate.rights_price
                and latest.terminal_price == candidate.terminal_price
                and latest.record_date == candidate.record_date
                and latest.announce_date == candidate.announce_date
                and latest.pay_date == candidate.pay_date
                and latest.stock_mkt_date == candidate.stock_mkt_date
            )
            if same:
                skipped += 1
                continue
            session.add(CorporateActionEvent(
                action_id=proposal.action_id, revision=(latest.revision + 1 if latest else 1),
                asset_code=candidate.asset_code, action_type=candidate.action_type,
                ex_date=candidate.ex_date, record_date=candidate.record_date,
                announce_date=candidate.announce_date, pay_date=candidate.pay_date,
                stock_mkt_date=candidate.stock_mkt_date, per_share_cash=candidate.per_share_cash,
                per_share_stock=candidate.per_share_stock, rights_ratio=candidate.rights_ratio,
                rights_price=candidate.rights_price, currency=candidate.currency,
                terminal_price=candidate.terminal_price,
                status=proposal.status, source_record_ids=source_ids,
                decision_note=proposal.decision_note,
                supersedes_event_id=latest.id if latest else None,
            ))
            added += 1
            accepted += int(proposal.status == "accepted")
            needs_review += int(proposal.status == "needs_review")
        session.commit()
    return {"added": added, "skipped": skipped, "accepted": accepted,
            "needs_review": needs_review}
