"""V2 调优后五套策略自选股荐股（价值/高股息/多因子/质量增强/盈利动量进攻）。

该入口只负责把 V2 单日评分器装配成“自选股荐股”语义：股票池取
``Asset.is_watch`` 且 ``asset_type=stock``，不把指数成分池或 ETF 混入。
评分本身仍复用 ``services.v2_signal.V2SignalScorer``，不读取持仓、不执行交易。
推荐榜单 = 综合均分前 top_n ∪ 每策略各自前 per_strategy_top，去重后按均分排序。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from sqlmodel import select

from stockfu.db import session_scope
from stockfu.models import Asset, QuoteSnapshot, SecurityMaster
from stockfu.services.universe import UniverseRules
from stockfu.services.v2_signal import V2SignalScorer

ROOT = Path(__file__).resolve().parents[2]
REPORT_DIR = ROOT / "data" / "reports" / "recommend"

# 调优后固定五套（final canonical，见 docs/SPECS/v2-tuning-results.md、
# quality-factor-validation-2026.md 与 growth-offense-gate-results.md）：
# 价值、高股息、多因子 + 质量增强多因子（multi_factor_quality_v2）+ 盈利动量进攻
# （earnings_momentum_offense_v2，vol8 配置）。
# 第四套为 2026-08-14 用户决策转正（"2020+ 近期增强候选"，三段门禁早期段受
# 预热数据限制未纳入正式保留集，但作为第四套荐股长期跟踪）。
# 第五套为 2026-08-15 用户决策纳入：vol8 全段总收益 +237.53% 距四套最差
# （dividend +321.49%）差距 26.1% < 30% 门槛，纳入正式荐股集合长期跟踪；
# 按三段门禁仍标"待验证"（2020-2026 Sharpe 微负），属配置决策非门禁转正。
# 各套为独立策略，评分仍逐套独立展示，均分仅用于自选池排序。
RECOMMENDATION_ALPHA_IDS: tuple[str, ...] = (
    "value_ep_bp_equal_v2",
    "dividend_income_history45_v2",
    "multi_factor_value_tilt_v2",
    "multi_factor_quality_v2",
    "earnings_momentum_offense_v2",
)


def watchlist_stock_codes(as_of: date | None = None) -> list[str]:
    """返回自选中的有效 A 股股票代码，排除 ETF/基金与已退市状态。"""
    with session_scope() as s:
        assets = s.exec(
            select(Asset).where(
                Asset.is_watch == True,  # noqa: E712
                Asset.market == "cn",
                Asset.asset_type == "stock",
            )
        ).all()
        codes = sorted({row.code for row in assets if row.code})
        if not codes:
            return []
        masters = {
            row.code: row
            for row in s.exec(
                select(SecurityMaster).where(SecurityMaster.code.in_(codes))
            ).all()
        }

    if as_of is None:
        return codes
    return [
        code for code in codes
        if (
            (masters.get(code) is None
             or masters[code].status in (None, "", "1"))
            and (
                masters.get(code) is None
                or masters[code].delist_date is None
                or as_of < masters[code].delist_date
            )
        )
    ]


def quote_coverage(codes: list[str], as_of: date) -> dict[str, Any]:
    """检查目标日行情覆盖，推荐入口对缺失股票 fail-closed。"""
    if not codes:
        return {"expected": 0, "present": 0, "missing": []}
    with session_scope() as s:
        present = {
            code for code in s.exec(
                select(QuoteSnapshot.asset_code).where(
                    QuoteSnapshot.quote_date == as_of,
                    QuoteSnapshot.asset_code.in_(codes),
                ).distinct()
            ).all()
        }
    return {
        "expected": len(codes),
        "present": len(present),
        "missing": sorted(set(codes) - present),
    }


_ALPHA_LABELS = {
    "value_ep_bp_equal_v2": "价值",
    "dividend_income_history45_v2": "高股息",
    "multi_factor_value_tilt_v2": "多因子",
    "multi_factor_quality_v2": "质量增强",
    "earnings_momentum_offense_v2": "盈利进攻",
}


def _short_alpha(alpha_id: str) -> str:
    """打印用短列名：已知策略用中文固定标签，未知回退移除后缀截断。"""
    return _ALPHA_LABELS.get(alpha_id, alpha_id.removesuffix("_v2")[:8])


def _rank_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """增加荐股视图字段；保留每个策略原始 0–100 分。"""
    out: list[dict[str, Any]] = []
    for row in rows:
        scores = row.get("scores") or {}
        tradable = [
            float(cell["score"])
            for cell in scores.values()
            if cell.get("status") == "tradable" and cell.get("score") is not None
        ]
        mean_score = round(sum(tradable) / len(tradable), 2) if tradable else None
        out.append({
            **row,
            "mean_score": mean_score,
            "n_scored": len(tradable),
            "n_bullish": sum(score >= 60.0 for score in tradable),
            "n_bearish": sum(score <= 40.0 for score in tradable),
        })
    out.sort(key=lambda row: (
        -(row["mean_score"] if row["mean_score"] is not None else -1.0),
        row["code"],
    ))
    for rank, row in enumerate(out, 1):
        row["rank"] = rank
        mean = row["mean_score"]
        row["recommendation"] = (
            "优先关注" if mean is not None and mean >= 60.0
            else "观察" if mean is not None and mean >= 50.0
            else "谨慎"
        )
    return out


def _build_recommend_list(
    rows: list[dict[str, Any]],
    alpha_ids: list[str],
    *,
    top_n: int = 30,
    per_strategy_top: int = 5,
) -> list[dict[str, Any]]:
    """综合推荐榜单 = 综合均分前 top_n ∪ 每策略各自前 per_strategy_top。

    - 入选理由记录在 ``inclusion``（如 ["综合前30", "价值前5"]，中文策略名）；
    - 去重（同一代码只出现一次，理由合并）后按综合均分降序重排、重编号 rank。
    输入 rows 须为 ``_rank_rows`` 的输出（含 mean_score）。
    """
    by_code = {row["code"]: row for row in rows}
    picked: dict[str, list[str]] = {}
    for row in rows[: max(0, top_n)]:
        picked.setdefault(row["code"], []).append(f"综合前{top_n}")
    for aid in alpha_ids:
        label = _ALPHA_LABELS.get(aid, _short_alpha(aid))
        scored = []
        for row in rows:
            cell = (row.get("scores") or {}).get(aid) or {}
            value = cell.get("score")
            if value is not None:
                scored.append((float(value), row["code"]))
        scored.sort(key=lambda t: (-t[0], t[1]))
        for _, code in scored[: max(0, per_strategy_top)]:
            picked.setdefault(code, []).append(f"{label}前{per_strategy_top}")
    out = []
    for code, reasons in picked.items():
        row = dict(by_code[code])
        row["inclusion"] = reasons
        out.append(row)
    out.sort(key=lambda row: (
        -(row["mean_score"] if row["mean_score"] is not None else -1.0),
        row["code"],
    ))
    for rank, row in enumerate(out, 1):
        row["rank"] = rank
    return out


def run_v2_watchlist_recommendation(
    as_of: date | str,
    *,
    alpha_ids: list[str] | None = None,
    top_n: int = 30,
    per_strategy_top: int = 5,
    save: bool = True,
) -> dict[str, Any]:
    """在自选股票范围运行 V2 调优后五套策略单日荐股并保存完整报告。

    报告含两部分：``rows``（自选池全量按均分排序，供核对）与 ``recommend_list``
    （综合推荐榜单 = 均分前 top_n ∪ 每策略各自前 per_strategy_top，去重后按均分排序）。
    """
    if isinstance(as_of, str):
        as_of = date.fromisoformat(as_of[:10])
    codes = watchlist_stock_codes(as_of)
    if not codes:
        raise ValueError("自选股为空（或没有有效的 A 股股票）")

    coverage = quote_coverage(codes, as_of)
    if coverage["missing"]:
        missing = ",".join(coverage["missing"][:20])
        suffix = "…" if len(coverage["missing"]) > 20 else ""
        raise ValueError(
            f"{as_of.isoformat()} 自选股行情不完整: "
            f"{coverage['present']}/{coverage['expected']}，缺失 {missing}{suffix}；"
            "请先运行每日抓取"
        )

    selected = list(alpha_ids or RECOMMENDATION_ALPHA_IDS)
    scorer = V2SignalScorer(
        alpha_ids=selected,
        # 自选股是显式池：不能再套用 HS300+CSI500 成分过滤；仍保留
        # 上市天数、交易状态、ST 和成交额等可投资性过滤。
        universe_rules=UniverseRules(
            universe_id="cn_watchlist_stock_v1",
            index_codes=(),
        ),
        codes=codes,
    )
    report = scorer.score(as_of)
    rows = _rank_rows(report.rows)
    recommend_list = _build_recommend_list(
        rows, selected, top_n=top_n, per_strategy_top=per_strategy_top
    )
    result: dict[str, Any] = {
        "mode": "v2_watchlist_recommendation",
        "as_of": report.as_of.isoformat(),
        "pool": "watchlist_stock",
        "pool_size": len(codes),
        "scored_size": report.n_scored,
        "quote_coverage": coverage,
        "strategy_selection": {
            "source": "docs/SPECS/v2-tuning-results.md + growth-offense-gate-results.md",
            "method": "调优后固定五套（价值/高股息/多因子/质量增强/盈利动量进攻），final canonical",
            "research_only": True,
        },
        "ranking_note": (
            f"推荐榜单 = 综合均分前 {top_n} ∪ 每策略各自前 {per_strategy_top}，"
            "去重后按综合均分降序；各策略分布不同，须结合逐策略分数和多空票数阅读。"
        ),
        "recommend_list": recommend_list,
        "alpha_ids": selected,
        "alpha_names": report.alpha_names,
        "calibration": report.calibration,
        "rows": rows,
    }
    if save:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = REPORT_DIR / f"{report.as_of.isoformat()}_v2_watchlist.json"
        path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        result["report_path"] = str(path)
    return result


def print_v2_watchlist_recommendation(result: dict[str, Any], *, top_n: int = 30) -> None:
    """打印推荐榜单（综合前 top_n ∪ 各策略前 5，按均分排序），完整结果已落盘。"""
    rows = result.get("recommend_list") or result.get("rows") or []
    alpha_ids = result.get("alpha_ids") or []
    note = result.get("ranking_note") or ""
    print(
        f"\nV2 自选股荐股榜单 · {result.get('as_of')} · "
        f"股票池 {result.get('pool_size')} 只 · 评分 {result.get('scored_size')} 只 · "
        f"榜单 {len(rows)} 只"
    )
    print(f"规则: {note}")
    print("排名  代码      名称        均分   多头/空头  结论   入选理由  "
          + " ".join(f"{_short_alpha(aid):>8}" for aid in alpha_ids))
    for row in rows[:max(0, top_n * 4)]:
        scores = row.get("scores") or {}
        cells = []
        for aid in alpha_ids:
            cell = scores.get(aid) or {}
            value = cell.get("score")
            cells.append(f"{float(value):8.1f}" if value is not None else f"{'—':>8}")
        inclusion = "/".join(row.get("inclusion") or [])
        print(
            f"{row.get('rank', 0):>4}  {row.get('code', ''):<8} "
            f"{(row.get('name') or '')[:8]:<8} "
            f"{(row.get('mean_score') if row.get('mean_score') is not None else 0):>6.1f} "
            f"{row.get('n_bullish', 0):>2}/{row.get('n_bearish', 0):<2}      "
            f"{row.get('recommendation', ''):<4} {inclusion:<16} " + " ".join(cells)
        )
    if result.get("report_path"):
        print(f"完整报告: {result['report_path']}")
