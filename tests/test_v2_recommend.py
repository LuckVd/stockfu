"""V2 自选股荐股装配测试。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from unittest import TestCase, mock

from sqlmodel import Session, SQLModel, create_engine

from stockfu.models import Asset, QuoteSnapshot, SecurityMaster
from stockfu.services import v2_recommend


def _scope_factory(engine):
    @contextmanager
    def scope():
        with Session(engine) as session:
            yield session
    return scope


class TestV2WatchlistAssembly(TestCase):
    def test_stock_filter_excludes_etf_and_inactive(self):
        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            session.add_all([
                Asset(code="600000", market="cn", asset_type="stock", is_watch=True),
                Asset(code="510300", market="cn", asset_type="fund_etf", is_watch=True),
                Asset(code="600001", market="cn", asset_type="stock", is_watch=True),
                Asset(code="600002", market="cn", asset_type="stock", is_watch=False),
            ])
            session.add(SecurityMaster(code="600001", status="0"))
            session.commit()
        with mock.patch.object(v2_recommend, "session_scope", _scope_factory(engine)):
            self.assertEqual(
                v2_recommend.watchlist_stock_codes(date(2026, 8, 11)), ["600000"]
            )

    def test_quote_coverage_reports_missing(self):
        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            session.add(QuoteSnapshot(asset_code="600000", quote_date=date(2026, 8, 11)))
            session.commit()
        with mock.patch.object(v2_recommend, "session_scope", _scope_factory(engine)):
            result = v2_recommend.quote_coverage(
                ["600000", "600001"], date(2026, 8, 11)
            )
        self.assertEqual(
            result, {"expected": 2, "present": 1, "missing": ["600001"]}
        )

    def test_rank_rows_keeps_strategy_scores_and_adds_consensus(self):
        rows = v2_recommend._rank_rows([
            {"code": "B", "scores": {"a": {"score": 60, "status": "tradable"}}},
            {"code": "A", "scores": {
                "a": {"score": 80, "status": "tradable"},
                "b": {"score": 40, "status": "tradable"},
            }},
        ])
        self.assertEqual([row["code"] for row in rows], ["A", "B"])
        self.assertEqual(rows[0]["mean_score"], 60.0)
        self.assertEqual(rows[0]["n_bullish"], 1)
        self.assertEqual(rows[0]["recommendation"], "优先关注")

    def test_recommendation_alpha_ids_are_tuned_suite(self):
        # 荐股链路必须用调优后五套（价值/高股息/多因子/质量增强/盈利动量进攻），
        # 不得回退到十策略全集
        self.assertEqual(
            v2_recommend.RECOMMENDATION_ALPHA_IDS,
            (
                "value_ep_bp_equal_v2",
                "dividend_income_history45_v2",
                "multi_factor_value_tilt_v2",
                    "multi_factor_quality_v2",
                "earnings_momentum_offense_v2",
            ),
        )

    def _row(self, code, scores):
        return {
            "code": code,
            "name": code,
            "scores": {
                aid: {"score": sc, "status": "tradable"}
                for aid, sc in scores.items()
            },
        }

    def test_build_recommend_list_union_dedupe_sort(self):
        alphas = ["value_ep_bp_equal_v2", "dividend_income_history45_v2"]
        rows = v2_recommend._rank_rows([
            self._row("A", {"value_ep_bp_equal_v2": 90.0, "dividend_income_history45_v2": 10.0}),
            self._row("B", {"value_ep_bp_equal_v2": 20.0, "dividend_income_history45_v2": 95.0}),
            self._row("C", {"value_ep_bp_equal_v2": 30.0, "dividend_income_history45_v2": 30.0}),
            self._row("D", {"value_ep_bp_equal_v2": 40.0, "dividend_income_history45_v2": 40.0}),
        ])
        # 均分: B 57.5 > A 50 > D 40 > C 30。top_n=2 → B、A；
        # 价值前 2 → A、D；高股息前 2 → B、D → 并集 B、A、D 按均分排序
        lst = v2_recommend._build_recommend_list(rows, alphas, top_n=2, per_strategy_top=2)
        self.assertEqual([r["code"] for r in lst], ["B", "A", "D"])
        self.assertEqual(lst[0]["inclusion"], ["综合前2", "高股息前2"])
        self.assertEqual(sorted(lst[1]["inclusion"]), ["价值前2", "综合前2"])
        self.assertEqual(sorted(lst[2]["inclusion"]), ["价值前2", "高股息前2"])

    def test_build_recommend_list_adds_strategy_picks(self):
        alphas = ["value_ep_bp_equal_v2", "dividend_income_history45_v2"]
        rows = v2_recommend._rank_rows([
            self._row("A", {"value_ep_bp_equal_v2": 90.0, "dividend_income_history45_v2": 10.0}),
            self._row("B", {"value_ep_bp_equal_v2": 20.0, "dividend_income_history45_v2": 95.0}),
            self._row("C", {"value_ep_bp_equal_v2": 80.0, "dividend_income_history45_v2": 20.0}),
            self._row("D", {"value_ep_bp_equal_v2": 70.0, "dividend_income_history45_v2": 30.0}),
        ])
        # 均分: B 57.5 > A/C/D 50(按 code)。top_n=1 → B；价值前 2 → A、C；
        # 高股息前 2 → B、D → 并集 B、A、C、D 按均分排序
        lst = v2_recommend._build_recommend_list(rows, alphas, top_n=1, per_strategy_top=2)
        self.assertEqual([r["code"] for r in lst], ["B", "A", "C", "D"])
        self.assertEqual(lst[0]["inclusion"], ["综合前1", "高股息前2"])
        self.assertEqual(lst[1]["inclusion"], ["价值前2"])
        self.assertEqual(lst[2]["inclusion"], ["价值前2"])
        self.assertEqual(lst[3]["inclusion"], ["高股息前2"])
        self.assertEqual([r["rank"] for r in lst], [1, 2, 3, 4])

    def test_alpha_display_names_are_chinese(self):
        from stockfu.services.v2_signal import ALPHA_CN_NAMES, _alpha_display_name
        from stockfu.strategy.alpha import alpha_from_dict
        from stockfu.backtest.v2_run import _load

        for aid in v2_recommend.RECOMMENDATION_ALPHA_IDS:
            self.assertIn(aid, ALPHA_CN_NAMES)
            alpha = alpha_from_dict(_load(f"alphas/{aid}.yaml"))
            self.assertEqual(_alpha_display_name(alpha), ALPHA_CN_NAMES[aid])
        # 中文名互不相同
        self.assertEqual(len(set(ALPHA_CN_NAMES.values())), len(ALPHA_CN_NAMES))

    def test_merge_watchlist_tags_listed_and_inserts_rest(self):
        alphas = ["value_ep_bp_equal_v2", "dividend_income_history45_v2"]
        list_rows = v2_recommend._build_recommend_list(
            v2_recommend._rank_rows([
                self._row("A", {"value_ep_bp_equal_v2": 90.0,
                                "dividend_income_history45_v2": 10.0}),
                self._row("B", {"value_ep_bp_equal_v2": 20.0,
                                "dividend_income_history45_v2": 95.0}),
            ]),
            alphas, top_n=2, per_strategy_top=2,
        )
        # 自选 C 均分 80 最高 → 插入榜首；A 已在榜单 → 只补「自选」标签、保留原分
        watch_rows = v2_recommend._rank_rows([
            self._row("C", {"value_ep_bp_equal_v2": 80.0,
                            "dividend_income_history45_v2": 80.0}),
            self._row("A", {"value_ep_bp_equal_v2": 1.0,
                            "dividend_income_history45_v2": 1.0}),
        ])
        merged, stats = v2_recommend.merge_watchlist_into_list(list_rows, watch_rows)
        self.assertEqual([r["code"] for r in merged], ["C", "B", "A"])
        self.assertEqual([r["rank"] for r in merged], [1, 2, 3])
        self.assertEqual(merged[0]["inclusion"], ["自选"])
        # A 保留宇宙池评分(均分 50),不用自选池的 1.0 分,只追加标签
        self.assertEqual(merged[2]["mean_score"], 50.0)
        self.assertIn("自选", merged[2]["inclusion"])
        self.assertEqual(stats["watch_total"], 2)
        self.assertEqual(stats["already_listed"], ["A"])
        self.assertEqual(stats["inserted"], ["C"])

    def test_merge_watchlist_tag_not_duplicated(self):
        alphas = ["value_ep_bp_equal_v2"]
        list_rows = v2_recommend._build_recommend_list(
            v2_recommend._rank_rows([
                self._row("A", {"value_ep_bp_equal_v2": 90.0}),
            ]),
            alphas, top_n=1, per_strategy_top=1,
        )
        list_rows[0]["inclusion"] = ["综合前1", "自选"]
        watch_rows = v2_recommend._rank_rows([
            self._row("A", {"value_ep_bp_equal_v2": 90.0}),
        ])
        merged, stats = v2_recommend.merge_watchlist_into_list(list_rows, watch_rows)
        self.assertEqual(merged[0]["inclusion"].count("自选"), 1)
        self.assertEqual(stats["inserted"], [])

    def test_signal_mail_html_marks_watchlist_rows(self):
        from stockfu.services.signal_mail_v2 import build_v2_signal_mail_html
        from stockfu.services.v2_signal import V2SignalReport

        report = V2SignalReport(
            as_of=date(2026, 9, 1),
            alpha_ids=["value_ep_bp_equal_v2"],
            alpha_names={"value_ep_bp_equal_v2": "价值"},
            universe_size=2,
            n_scored=2,
            rows=[
                {"code": "A", "name": "甲",
                 "scores": {"value_ep_bp_equal_v2": {"score": 90.0, "status": "tradable"}}},
                {"code": "B", "name": "乙",
                 "scores": {"value_ep_bp_equal_v2": {"score": 40.0, "status": "tradable"}}},
            ],
            calibration={},
        )
        list_rows = [
            {"code": "B", "name": "乙", "rank": 1, "mean_score": 40.0,
             "inclusion": ["综合前1", "自选"],
             "scores": {"value_ep_bp_equal_v2": {"score": 40.0, "status": "tradable"}}},
        ]
        html_doc = build_v2_signal_mail_html(
            report, top_n=1, list_rows=list_rows
        )
        self.assertIn("watch-tag", html_doc)
        self.assertIn("自选", html_doc)
        self.assertIn("∪ 自选 1 只", html_doc)
