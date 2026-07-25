"""历史指数成分股宇宙。

“并集”只决定需准备哪些证券数据；每天实际参与者严格取
``effective_from <= T < effective_to``。没有正式历史档案时保留缺口，不把今天
的 constituent 文件倒灌到历史。
"""
from __future__ import annotations

from collections import defaultdict
import csv
from datetime import date
from io import StringIO
from typing import Iterable

from sqlalchemy import func
from sqlmodel import select

from stockfu.db import session_scope
from stockfu.models import IndexConstituent

# 当前正式回测宇宙：沪深300 + 中证1000。创业板指/科创50可保留归档数据，
# 但不参与默认策略宇宙，避免未补齐的公告链影响回测口径。
HISTORICAL_INDEX_CODES = ("000300", "000852")
HISTORICAL_UNIVERSE_ID = "cn_historical_csi300_csi1000_v1"
INDEX_INCEPTION = {
    "000300": date(2006, 1, 1), "000852": date(2014, 10, 17),
    "399006": date(2010, 6, 1), "000688": date(2020, 7, 23),
}
SSE_STAR50_INITIAL_URL = (
    "https://www.sse.com.cn/market/sseindex/diclosure/c/10077925/"
    "files/1e710951ab8d4f0997e3737eee6ebc86.xlsx"
)


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


def import_adjustment(index_code: str, *, added: Iterable[str], removed: Iterable[str],
                      effective_from: date, announce_date: date | None,
                      source: str, source_ref: str) -> dict:
    """把一份正式调样公告变为该生效日的完整快照。

    公告通常只给调入/调出；数据库始终保存完整集合。导入前严格校验调出项在
    上期内、调入项不在上期内，避免错读表格列或跨指数混入后静默污染历史。
    """
    index_code = normalize_code(index_code)
    with session_scope() as s:
        prior_date = s.exec(select(func.max(IndexConstituent.effective_from)).where(
            IndexConstituent.index_code == index_code,
            IndexConstituent.effective_from < effective_from,
        )).one()
        if prior_date is None:
            raise ValueError(f"{index_code} {effective_from} 缺少上期完整快照")
        prior = set(s.exec(select(IndexConstituent.asset_code).where(
            IndexConstituent.index_code == index_code,
            IndexConstituent.effective_from == prior_date,
        )).all())
    add_set = {normalize_code(code) for code in added if normalize_code(code)}
    remove_set = {normalize_code(code) for code in removed if normalize_code(code)}
    if not add_set or not remove_set:
        raise ValueError("调样公告的调入或调出列表为空，拒绝导入")
    if add_set & prior:
        raise ValueError(f"调入项已在上期成分中: {sorted(add_set & prior)}")
    if remove_set - prior:
        raise ValueError(f"调出项不在上期成分中: {sorted(remove_set - prior)}")
    members = (prior - remove_set) | add_set
    if len(members) != len(prior) - len(remove_set) + len(add_set):
        raise ValueError("调样后成员数异常")
    return import_snapshot(index_code, members, effective_from=effective_from,
                           announce_date=announce_date, source=source, source_ref=source_ref)


def import_sse_star50_initial_snapshot() -> dict:
    """从上交所 2020-06-19 公告附件导入科创50的正式初始样本。"""
    from io import BytesIO
    import pandas as pd
    import requests
    from stockfu.data.base import direct_connection

    with direct_connection():
        response = requests.get(SSE_STAR50_INITIAL_URL, timeout=30)
    response.raise_for_status()
    df = pd.read_excel(BytesIO(response.content))
    code_column = next((name for name in df.columns if "证券代码" in str(name)), None)
    if code_column is None:
        raise RuntimeError(f"上交所初始名单列结构变化: {list(df.columns)}")
    members = df[code_column].tolist()
    if len({normalize_code(code) for code in members}) != 50:
        raise RuntimeError(f"上交所初始名单成员数异常: {len(members)}")
    return import_snapshot("000688", members, effective_from=date(2020, 7, 23),
                           announce_date=date(2020, 6, 19),
                           source="sse_official_initial_constituents",
                           source_ref=SSE_STAR50_INITIAL_URL)


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


def _snapshot_members_at_or_before(index_code: str, as_of: date) -> set[str]:
    """返回最近一份完整快照的成员，不把已结束的区间误当作当日成员。"""
    with session_scope() as s:
        latest = s.exec(select(func.max(IndexConstituent.effective_from)).where(
            IndexConstituent.index_code == index_code,
            IndexConstituent.effective_from <= as_of,
        )).one()
        if latest is None:
            return set()
        return set(s.exec(select(IndexConstituent.asset_code).where(
            IndexConstituent.index_code == index_code,
            IndexConstituent.effective_from == latest,
        )).all())


def fetch_baostock_hs300_snapshot(as_of: date) -> set[str]:
    """取得 BaoStock 给出的某交易日沪深 300 成分。

    这是可复现的历史接口，但不是指数公司正式档案。因此写入时会明确标记为
    ``unverified``；正式中证样本文件到位后可进行逐期交叉核验，而不会混淆来源。
    """
    from stockfu.data.baostock_proxy import ensure_baostock_login
    import baostock as bs

    if not ensure_baostock_login():
        raise RuntimeError("baostock 登录失败")
    result = bs.query_hs300_stocks(date=as_of.isoformat())
    if result.error_code != "0":
        raise RuntimeError(f"baostock query_hs300_stocks: {result.error_code} {result.error_msg}")
    fields = {name: i for i, name in enumerate(result.fields)}
    code_pos = fields.get("code")
    if code_pos is None:
        raise RuntimeError(f"baostock 字段异常: {result.fields}")
    members: set[str] = set()
    while result.next():
        raw = result.get_row_data()[code_pos]
        members.add(normalize_code(raw.rsplit(".", 1)[-1]))
    if len(members) != 300:
        raise RuntimeError(f"baostock {as_of} 返回成员数 {len(members)}，期望 300")
    return members


def backfill_baostock_hs300(*, start: date, end: date) -> dict:
    """按本地上证交易日扫描沪深300，仅在成分变化日落完整快照。"""
    from stockfu.models import IndexQuoteDaily

    with session_scope() as s:
        trade_dates = s.exec(select(IndexQuoteDaily.quote_date).where(
            IndexQuoteDaily.asset_code == "sh000001",
            IndexQuoteDaily.quote_date >= start,
            IndexQuoteDaily.quote_date <= end,
        ).order_by(IndexQuoteDaily.quote_date)).all()
    if not trade_dates:
        raise RuntimeError("缺少 sh000001 交易日历；请先运行 --backfill-benchmark")
    out = {"scanned": 0, "imported": 0, "unchanged": 0, "errors": []}
    known: set[str] | None = None
    for as_of in trade_dates:
        out["scanned"] += 1
        try:
            members = fetch_baostock_hs300_snapshot(as_of)
            baseline = known if known is not None else _snapshot_members_at_or_before("000300", as_of)
            if members == baseline:
                out["unchanged"] += 1
            else:
                import_snapshot("000300", members, effective_from=as_of,
                                source="baostock_hs300_snapshot_unverified",
                                source_ref=f"baostock://query_hs300_stocks?date={as_of.isoformat()}")
                out["imported"] += 1
            known = members
        except Exception as exc:  # noqa: BLE001
            out["errors"].append({"date": as_of.isoformat(), "error": f"{type(exc).__name__}: {exc}"})
    return out


def _month_starts(start: date, end: date) -> list[date]:
    current = date(start.year, start.month, 1)
    months: list[date] = []
    while current <= end:
        months.append(current)
        current = date(current.year + (current.month == 12), current.month % 12 + 1, 1)
    return months


def fetch_yfiua_monthly_snapshot(index_name: str, as_of: date) -> set[str] | None:
    """下载公开月度成分镜像，缺文件时返回 None。

    镜像只保证月度时点，不能替代指数公司对临时调样的正式公告；调用方必须将
    它保留为 ``unverified`` 来源，且不可据此宣称日级完整性。
    """
    import requests
    from stockfu.data.base import direct_connection

    url = ("https://yfiua.github.io/index-constituents/"
           f"{as_of.year:04d}/{as_of.month:02d}/constituents-{index_name}.csv")
    with direct_connection():
        response = requests.get(url, timeout=30)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    rows = csv.DictReader(StringIO(response.text))
    members = {normalize_code((row.get("Symbol") or "").split(".", 1)[0]) for row in rows}
    members.discard("")
    if not members:
        raise RuntimeError(f"月度镜像为空: {url}")
    return members


def backfill_yfiua_csi1000(*, start: date, end: date) -> dict:
    """回补镜像可得的中证1000月度快照（当前从 2025-04 起）。"""
    out = {"scanned": 0, "imported": 0, "unchanged": 0, "missing": 0, "errors": []}
    known: set[str] | None = None
    for as_of in _month_starts(start, end):
        out["scanned"] += 1
        try:
            members = fetch_yfiua_monthly_snapshot("csi1000", as_of)
            if members is None:
                out["missing"] += 1
                continue
            if len(members) != 1000:
                raise RuntimeError(f"{as_of} 返回成员数 {len(members)}，期望 1000")
            baseline = known if known is not None else _snapshot_members_at_or_before("000852", as_of)
            if members == baseline:
                out["unchanged"] += 1
            else:
                url = ("https://yfiua.github.io/index-constituents/"
                       f"{as_of.year:04d}/{as_of.month:02d}/constituents-csi1000.csv")
                import_snapshot("000852", members, effective_from=as_of,
                                source="yfiua_csi1000_monthly_mirror_unverified",
                                source_ref=url)
                out["imported"] += 1
            known = members
        except Exception as exc:  # noqa: BLE001
            out["errors"].append({"date": as_of.isoformat(), "error": f"{type(exc).__name__}: {exc}"})
    return out


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
