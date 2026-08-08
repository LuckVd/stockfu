from datetime import date
from unittest.mock import patch

from stockfu.factors.raw.dividend import compute_dividend_yield_ttm
from stockfu.scoring.contracts import MissingReason
from stockfu.services.dividend import DividendYieldDetail


AS_OF = date(2024, 6, 3)


def test_no_cash_dividend_is_valid_zero_not_missing():
    with patch(
        "stockfu.services.dividend.dividend_yield_ttm_detail",
        return_value=DividendYieldDetail(
            yield_pct=None, ttm_cash_per_share=0.0, event_count=0),
    ):
        obs = compute_dividend_yield_ttm("600001", AS_OF)

    assert obs.valid is True
    assert obs.raw_value == 0.0
    assert obs.missing_reason is None
    assert obs.diagnostics["no_cash_dividend"] is True


def test_dividend_event_with_missing_price_stays_invalid():
    with patch(
        "stockfu.services.dividend.dividend_yield_ttm_detail",
        return_value=DividendYieldDetail(
            yield_pct=None, ttm_cash_per_share=0.5, event_count=1,
            price_missing=True),
    ):
        obs = compute_dividend_yield_ttm("600001", AS_OF)

    assert obs.valid is False
    assert obs.raw_value is None
    assert obs.missing_reason == MissingReason.FIELD_MISSING


def test_dividend_event_with_nonpositive_price_has_specific_reason():
    with patch(
        "stockfu.services.dividend.dividend_yield_ttm_detail",
        return_value=DividendYieldDetail(
            yield_pct=None, ttm_cash_per_share=0.5, event_count=1,
            price_nonpositive=True),
    ):
        obs = compute_dividend_yield_ttm("600001", AS_OF)

    assert obs.valid is False
    assert obs.missing_reason == MissingReason.NONPOSITIVE_DENOMINATOR


def test_dividend_raw_fingerprint_changes_with_zero_no_dividend_policy():
    with patch(
        "stockfu.services.dividend.dividend_yield_ttm_detail",
        return_value=DividendYieldDetail(
            yield_pct=5.0, ttm_cash_per_share=0.5, event_count=1),
    ):
        obs = compute_dividend_yield_ttm("600001", AS_OF)
        same_policy = compute_dividend_yield_ttm(
            "600001", AS_OF, no_dividend_policy="zero")

    assert obs.raw_fingerprint
    assert obs.raw_fingerprint == same_policy.raw_fingerprint


def test_empty_backtest_dividend_window_is_a_valid_zero():
    from stockfu.services.dividend import (
        clear_backtest_dividend_provider,
        dividend_yield_ttm_detail,
        set_backtest_dividend_provider,
    )

    try:
        set_backtest_dividend_provider(lambda code, start, end: [])
        detail = dividend_yield_ttm_detail("600001", AS_OF)
    finally:
        clear_backtest_dividend_provider()

    assert detail.ttm_cash_per_share == 0.0
    assert detail.event_count == 0
    assert detail.yield_pct is None
