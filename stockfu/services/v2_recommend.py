"""V2 调优后三套策略自选股荐股（价值/高股息/多因子）。

该入口只负责把 V2 单日评分器装配成“自选股荐股”语义：股票池取
``Asset.is_watch`` 且 ``asset_type=stock``，不把指数成分池或 ETF 混入。
评分本身仍复用 ``services.v2_signal.V2SignalScorer``，不读取持仓、不执行交易。
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

# 调优后固定三套（final canonical，见 docs/SPECS/v2-tuning-results.md）：
# 价值、高股息、多因子。三套为独立策略，评分仍逐套独立展示，均分仅用于自选池排序。
RECOMMENDATION_ALPHA_IDS: tuple[str, ...] = (
    "value_ep_bp_equal_v2",
    "dividend_income_history45_v2",
    "multi_factor_value_tilt_v2",
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


def run_v2_watchlist_recommendation(
    as_of: date | str,
    *,
    alpha_ids: list[str] | None = None,
    save: bool = True,
) -> dict[str, Any]:
    """在自选股票范围运行 V2 调优后三套策略单日荐股并保存完整报告。"""
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
    result: dict[str, Any] = {
        "mode": "v2_watchlist_recommendation",
        "as_of": report.as_of.isoformat(),
        "pool": "watchlist_stock",
        "pool_size": len(codes),
        "scored_size": report.n_scored,
        "quote_coverage": coverage,
        "strategy_selection": {
            "source": "docs/SPECS/v2-tuning-results.md",
            "method": "调优后固定三套（价值/高股息/多因子），final canonical",
            "research_only": True,
        },
        "ranking_note": (
            "均分仅用于本次自选池排序；各策略分布不同，须结合逐策略分数和多空票数阅读。"
        ),
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
    """打印可读的自选股荐股表，同时完整结果已落盘。"""
    rows = result.get("rows") or []
    alpha_ids = result.get("alpha_ids") or []
    print(
        f"\nV2 自选股荐股 · {result.get('as_of')} · "
        f"股票池 {result.get('pool_size')} 只 · 评分 {result.get('scored_size')} 只"
    )
    print("排名  代码      名称        均分   多头/空头  结论  "
          + " ".join(f"{aid.removesuffix('_v2')[:8]:>8}" for aid in alpha_ids))
    for row in rows[:max(0, top_n)]:
        scores = row.get("scores") or {}
        cells = []
        for aid in alpha_ids:
            cell = scores.get(aid) or {}
            value = cell.get("score")
            cells.append(f"{float(value):8.1f}" if value is not None else f"{'—':>8}")
        print(
            f"{row.get('rank', 0):>4}  {row.get('code', ''):<8} "
            f"{(row.get('name') or '')[:8]:<8} "
            f"{(row.get('mean_score') if row.get('mean_score') is not None else 0):>6.1f} "
            f"{row.get('n_bullish', 0):>2}/{row.get('n_bearish', 0):<2}      "
            f"{row.get('recommendation', ''):<4} " + " ".join(cells)
        )
    if result.get("report_path"):
        print(f"完整报告: {result['report_path']}")
