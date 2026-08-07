"""V2 协议回归：成熟门禁、expanding、参数绑定、确定性和行业上限。"""
from __future__ import annotations

from datetime import date

import pytest

from stockfu.backtest.v2_run import (
    build_v2_config, historical_hs300_universe_rules, hs300_universe,
)
from stockfu.backtest.v2_engine import _validate_raw_observation
from stockfu.scoring.contracts import (
    FactorScoreObservation,
    Maturity,
    ScoreStatus,
)
from stockfu.scoring.history import HistoryState
from stockfu.scoring.profiles import profile_from_dict
from stockfu.scoring.scorer import FactorScorer
from stockfu.scoring.contracts import RawFactorObservation
from stockfu.strategy.alpha import AlphaAggregator, AlphaDefinition, AlphaFactor
from stockfu.strategy.portfolio import (
    DayContext,
    PortfolioConstructor,
    portfolio_from_dict,
)


def _raw(value: float) -> RawFactorObservation:
    return RawFactorObservation(
        asset_code="A", as_of=date(2023, 1, 3), raw_metric_id="m",
        raw_value=value, raw_unit="ratio", source_max_date=date(2023, 1, 3),
        available_at=date(2023, 1, 3), valid=True, raw_fingerprint="raw",
    )


def test_expanding_history_ignores_rolling_year_cutoff():
    p = profile_from_dict({
        "profile_id": "m_expanding_v1", "version": 1,
        "raw_metric": {"id": "m", "params": {}},
        "direction": "higher_is_better", "raw_unit": "ratio",
        "mapping": {"mode": "hybrid", "components": {
            "self_history": {"weight": 1.0, "state": "expanding",
                              "years": 1, "sampling": "daily", "min_observations": 1},
        }},
    })
    h = HistoryState()
    h.update(date(2020, 1, 2), {"m": {"A": 1.0}}, {}, "cn",
             {"m": {"self": True}})
    fs = FactorScorer(p).score(_raw(2.0), h, None, "cn", date(2023, 1, 3))
    assert fs.history_n["self_history"] == 1
    assert fs.self_history_score == pytest.approx(100.0)


def test_formal_maturity_gate_blocks_partial_factor():
    alpha = AlphaDefinition(
        alpha_id="a", version=1, market_scope="cn",
        factors=(AlphaFactor("m", 1.0, False),),
        minimum_coverage=0.5, minimum_valid_factor_count=1,
    )
    fs = FactorScoreObservation(
        profile_id="m", profile_version=1, asset_code="A", as_of=date(2023, 1, 3),
        raw_metric_id="m", score=80.0, evidence_coverage=1.0,
        maturity=Maturity.PARTIAL, mapping_fingerprint="map",
        reference_cutoff=date(2023, 1, 2), formal_requires_mature=True,
    )
    out = AlphaAggregator(alpha).aggregate(
        "A", date(2023, 1, 3), {"m": fs}, reference_cutoff=date(2023, 1, 2))
    assert out.score_status == ScoreStatus.NOT_TRADABLE
    assert "尚未成熟" in out.reasons[0]


def test_portfolio_ties_are_code_deterministic_and_industry_capped():
    policy = portfolio_from_dict({
        "portfolio_policy_id": "p", "version": 1, "rebalance": "daily",
        "selection": {"method": "top_n_above_score", "n": 3, "minimum_score": 0},
        "weighting": "equal", "max_single_weight": 1.0, "max_gross": 1.0,
        "min_amount_20d": 0, "minimum_listing_days": 0, "max_industry_weight": 0.4,
    })
    alpha = AlphaDefinition(
        alpha_id="a", version=1, market_scope="cn",
        factors=(AlphaFactor("m", 1.0, False),), minimum_coverage=0,
        minimum_valid_factor_count=0,
    )
    scores = {}
    for code in ("C", "A", "B"):
        scores[code] = AlphaAggregator(alpha).aggregate(
            code, date(2023, 1, 3), {}, reference_cutoff=date(2023, 1, 2))
        scores[code].score_status = ScoreStatus.TRADABLE
        scores[code].strategy_score = 80.0
    ctx = DayContext(
        price={c: 10.0 for c in scores}, amount_20d={}, listing_date={},
        is_st={c: False for c in scores}, industry={c: "bank" for c in scores},
    )
    weights = PortfolioConstructor(policy).select_target(scores, ctx, date(2023, 1, 3))
    assert list(weights) == ["A", "B", "C"]
    assert sum(weights.values()) == pytest.approx(0.4)
    assert max(weights.values()) <= 0.4 / 3 + 1e-12


def test_profile_parameters_are_bound_to_raw_metric():
    cfg = build_v2_config(
        "low_beta_dividend_v2", "cn_equity_top5_v1", "no_overlay_v1",
        ["600519"], date(2021, 1, 1), date(2021, 2, 1), date(2018, 1, 1),
        observation_count=1,
    )
    assert cfg.market_scope == "cn_equity"
    assert cfg.raw_params["low_beta"] == {
        "window": 120, "bench": "sh000300", "price_basis": "qfq",
    }
    assert cfg.raw_params["value"] == {"years": 5}


def test_raw_contract_rejects_future_source_and_wrong_metric():
    raw = RawFactorObservation(
        asset_code="A", as_of=date(2023, 1, 3), raw_metric_id="other",
        raw_value=1.0, raw_unit="ratio", source_max_date=date(2023, 1, 4),
        available_at=date(2023, 1, 3), valid=True, raw_fingerprint="raw",
    )
    with pytest.raises(ValueError, match="raw metric 不匹配"):
        _validate_raw_observation(raw, "m", date(2023, 1, 3), "ratio", "A")

    raw.raw_metric_id = "m"
    with pytest.raises(ValueError, match="source_max_date"):
        _validate_raw_observation(raw, "m", date(2023, 1, 3), "ratio", "A")

    raw.source_max_date = date(2023, 1, 3)
    raw.available_at = None
    with pytest.raises(ValueError, match="available_at"):
        _validate_raw_observation(raw, "m", date(2023, 1, 3), "ratio", "A")


def test_raw_contract_rejects_wrong_asset_valid_without_value_and_empty_fingerprint():
    """§5.1 契约：asset 必须与请求一致；valid 必须有值；指纹必须非空；
    invalid 必须带 missing_reason（阻塞项③：raw 校验过松）。"""
    base = dict(
        asset_code="A", as_of=date(2023, 1, 3), raw_metric_id="m",
        raw_value=1.0, raw_unit="ratio", source_max_date=date(2023, 1, 3),
        available_at=date(2023, 1, 3), valid=True, raw_fingerprint="raw",
    )

    wrong_asset = RawFactorObservation(**{**base, "asset_code": "B"})
    with pytest.raises(ValueError, match="raw asset 不匹配"):
        _validate_raw_observation(wrong_asset, "m", date(2023, 1, 3), "ratio", "A")

    valid_no_value = RawFactorObservation(**{**base, "raw_value": None})
    with pytest.raises(ValueError, match="valid raw 必须带 raw_value"):
        _validate_raw_observation(valid_no_value, "m", date(2023, 1, 3), "ratio", "A")

    empty_fp = RawFactorObservation(**{**base, "raw_fingerprint": ""})
    with pytest.raises(ValueError, match="raw_fingerprint 不能为空"):
        _validate_raw_observation(empty_fp, "m", date(2023, 1, 3), "ratio", "A")

    invalid_no_reason = RawFactorObservation(
        **{**base, "valid": False, "raw_value": None, "missing_reason": None})
    with pytest.raises(ValueError, match="missing_reason"):
        _validate_raw_observation(invalid_no_reason, "m", date(2023, 1, 3), "ratio", "A")

    # 合法观测必须通过。
    _validate_raw_observation(
        RawFactorObservation(**base), "m", date(2023, 1, 3), "ratio", "A")


def test_v2_hs300_universe_uses_historical_member_union(monkeypatch):
    monkeypatch.setattr(
        "stockfu.services.index_universe.historical_member_codes",
        lambda index_codes: ["600001", "600002"],
    )
    assert hs300_universe() == ["600001", "600002"]


def test_v2_hs300_rules_enable_daily_historical_membership():
    rules = historical_hs300_universe_rules()
    assert rules.universe_id == "cn_historical_baostock_csi300_v1"
    assert rules.index_codes == ("000300",)

    cfg = build_v2_config(
        "dividend_low_vol_v2", "cn_equity_top15_v2", "no_overlay_v1",
        ["600001"], date(2021, 1, 1), date(2021, 2, 1), date(2018, 1, 1),
        observation_count=1, universe_rules=rules,
    )
    assert cfg.universe_rules.to_dict()["index_codes"] == ("000300",)
    assert cfg.manifest()["universe_rules"]["universe_id"] == "cn_historical_baostock_csi300_v1"


def test_build_v2_config_manifest_includes_raw_fingerprints():
    """真实配置下 raw 算法指纹进入 manifest（阻塞项②：checkpoint identity 含 raw 算法）。"""
    cfg = build_v2_config(
        "dividend_low_vol_v2", "cn_equity_top15_v2", "no_overlay_v1",
        ["600001"], date(2021, 1, 1), date(2021, 2, 1), date(2018, 1, 1),
        observation_count=1,
    )
    fps = cfg.manifest()["raw_metric_fingerprints"]
    assert set(fps) == {"dividend_yield_ttm", "low_volatility_20d"}
    assert all(len(fp) == 64 for fp in fps.values())
    # 参数变化 → 指纹变化（换口径必须换 identity）。
    cfg2 = build_v2_config(
        "dividend_low_vol_v2", "cn_equity_top15_v2", "no_overlay_v1",
        ["600001"], date(2021, 1, 1), date(2021, 2, 1), date(2018, 1, 1),
        observation_count=1,
    )
    cfg2.raw_params["dividend_yield_ttm"] = {"price_basis": "raw", "trailing_days": 180}
    cfg2.raw_fingerprints["dividend_yield_ttm"] = "x" * 64
    assert cfg.checkpoint_identity() != cfg2.checkpoint_identity()
