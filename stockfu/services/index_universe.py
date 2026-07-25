"""历史指数成分股宇宙。

“并集”只决定需准备哪些证券数据；每天实际参与者严格取
``effective_from <= T < effective_to``。没有正式历史档案时保留缺口，不把今天
的 constituent 文件倒灌到历史。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Iterable

from sqlalchemy import func
from sqlmodel import select

from stockfu.db import session_scope
from stockfu.models import IndexConstituent

HISTORICAL_INDEX_CODES = ("000300", "000852", "399006", "000688")
HISTORICAL_UNIVERSE_ID = "cn_historical_indices_v1"
INDEX_INCEPTION = {
    "000300": date(2006, 1, 1), "000852": date(2014, 10, 17),
    "399006": date(2010, 6, 1), "000688": date(2020, 7, 23),
}


def normalize_code(raw: object) -> str:
    s = str(raw or "").strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s.zfill(6) if s.isdigit() and len(s) <= 6 else s


def normalize_index_codes(codes: Iterable[str] | None = None) -> tuple[str, ...]:
    raw = codes or HISTORICAL_INDEX_CODES
    return tuple(sorted({normalize_code(c) for c in raw if normalize_code(c)}))


def historical_member_codes(index_codes: Iterable[str] | None = None) -> list[str]:
    wanted = normalize_index_codes(index_codes)
    with session_scope() as s:
        rows = s.exec(select(IndexConstituent.asset_code).where(
            IndexConstituent.index_code.in_(wanted)).distinct()).all()
    return sorted({c for c in rows if c})


def memberships_for(codes: Iterable[str], index_codes: Iterable[str] | None = None) -> dict[str, list[tuple[date, date | None]]]:
    code_list = sorted({normalize_code(c) for c in codes if normalize_code(c)})
    wanted = normalize_index_codes(index_codes)
    if not code_list:
        return {}
    with session_scope() as s:
        rows = s.exec(select(IndexConstituent).where(
            IndexConstituent.index_code.in_(wanted), IndexConstituent.asset_code.in_(code_list),
        ).order_by(IndexConstituent.asset_code, IndexConstituent.effective_from)).all()
    grouped: dict[str, list[tuple[date, date | None]]] = defaultdict(list)
    for row in rows:
        grouped[row.asset_code].append((row.effective_from, row.effective_to))
    merged: dict[str, list[tuple[date, date | None]]] = {}
    for code, spans in grouped.items():
        out: list[tuple[date, date | None]] = []
        for start, end in sorted(spans):
            if out and start <= (out[-1][1] or date.max):
                old_start, old_end = out[-1]
                if old_end is None or end is None:
                    out[-1] = (old_start, None)
                elif end > old_end:
                    out[-1] = (old_start, end)
            else:
                out.append((start, end))
        merged[code] = out
    return merged


def member_on(spans: list[tuple[date, date | None]], as_of: date) -> bool:
    return any(start <= as_of and (end is None or as_of < end) for start, end in spans)


def import_snapshot(index_code: str, members: Iterable[str], *, effective_from: date,
                    announce_date: date | None = None, source: str, source_ref: str) -> dict:
    """导入一份完整正式快照，维护该指数的连续有效区间。

    每期都保存完整成员集而非只保存变动项，故可安全按任意顺序补老档案，也能
    对同一生效日做内容校验。相邻且相同成员在读取时会合并为一个可交易区间。
    """
    index_code = normalize_code(index_code)
    member_set = {normalize_code(c) for c in members if normalize_code(c)}
    if not member_set:
        raise ValueError(f"{index_code} 的成分快照为空，拒绝写入")
    with session_scope() as s:
        same_date = s.exec(select(IndexConstituent).where(
            IndexConstituent.index_code == index_code,
            IndexConstituent.effective_from == effective_from)).all()
        if same_date:
            existing = {r.asset_code for r in s.exec(select(IndexConstituent).where(
                IndexConstituent.index_code == index_code,
                IndexConstituent.effective_from == effective_from)).all()}
            if existing != member_set:
                raise ValueError(f"{index_code} {effective_from} 已有不同快照，拒绝静默覆盖")
            return {"index_code": index_code, "effective_from": str(effective_from), "members": len(member_set), "added": 0, "closed": 0, "status": "exists"}
        # 上一期的所有成员到本期前一日失效；本期到下一份已知快照前有效。
        prior = s.exec(select(IndexConstituent).where(
            IndexConstituent.index_code == index_code,
            IndexConstituent.effective_from < effective_from,
            (IndexConstituent.effective_to.is_(None) | (IndexConstituent.effective_to > effective_from)),
        )).all()
        next_date = s.exec(select(func.min(IndexConstituent.effective_from)).where(
            IndexConstituent.index_code == index_code,
            IndexConstituent.effective_from > effective_from)).one()
        for row in prior:
            if row.effective_to is None or row.effective_to > effective_from:
                row.effective_to = effective_from
        for code in sorted(member_set):
            s.add(IndexConstituent(index_code=index_code, asset_code=code,
                  effective_from=effective_from, announce_date=announce_date,
                  effective_to=next_date, source=source, source_ref=source_ref))
        s.commit()
    return {"index_code": index_code, "effective_from": str(effective_from), "members": len(member_set), "added": len(member_set), "closed": len(prior), "status": "imported"}


def fetch_official_current_snapshot(index_code: str) -> dict:
    """抓中证正式 current 文件，仅按文件自身日期写入。"""
    import akshare as ak
    from stockfu.data.base import direct_connection
    code = normalize_code(index_code)
    # 运行环境可能预设海外代理；中证 OSS 必须直连，避免错误走本机 7890。
    with direct_connection():
        df = ak.index_stock_cons_csindex(symbol=code)
    if df is None or df.empty or "日期" not in df or "成分券代码" not in df:
        raise RuntimeError(f"中证文件为空或列结构变化: {code}")
    dates = {v for v in df["日期"].tolist() if isinstance(v, date)}
    if len(dates) != 1:
        raise RuntimeError(f"中证文件未给出唯一快照日期: {dates}")
    ref = f"https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/cons/{code}cons.xls"
    return import_snapshot(code, df["成分券代码"].tolist(), effective_from=next(iter(dates)),
                           source="csindex_current_constituent", source_ref=ref)


def audit_coverage(index_codes: Iterable[str] | None = None) -> dict:
    wanted = normalize_index_codes(index_codes)
    out = {"universe_id": HISTORICAL_UNIVERSE_ID, "indices": {}, "union_size": len(historical_member_codes(wanted))}
    with session_scope() as s:
        for code in wanted:
            rows = s.exec(select(IndexConstituent).where(IndexConstituent.index_code == code)).all()
            starts = [r.effective_from for r in rows]
            first = min(starts) if starts else None
            out["indices"][code] = {"inception": INDEX_INCEPTION[code].isoformat(), "rows": len(rows),
                "unique_members": len({r.asset_code for r in rows}),
                "first_effective": first.isoformat() if first else None,
                "last_effective": max(starts).isoformat() if starts else None,
                "historical_gap_before_first": not first or first > INDEX_INCEPTION[code]}
    return out
