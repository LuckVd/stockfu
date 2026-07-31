from collections import deque

import pytest

from stockfu.backtest.engine import (
    _update_atr_percent,
    atr_take_profit_action,
    tiered_take_profit_action,
    tiered_take_profit_reason,
)


def test_take_profit_is_disabled_without_config():
    assert tiered_take_profit_reason(10, 13, 12) is None


def test_take_profit_trailing_tiers_use_highest_reached_tier():
    tiers = ((0.20, 0.05), (0.30, 0.03))
    assert tiered_take_profit_reason(10, 12, 11.4, tiers) == "take_profit_trailing_0.2_0.05"
    assert tiered_take_profit_reason(10, 13, 12.6, tiers) == "take_profit_trailing_0.3_0.03"


def test_take_profit_hard_threshold_has_priority():
    tiers = ((0.20, 0.05), (0.30, 0.03))
    assert tiered_take_profit_reason(10, 15, 15, tiers, 0.50) == "take_profit_hard_0.5"


def test_partial_take_profit_fires_each_tier_once():
    tiers = ((0.20, 0.05, 1 / 3), (0.30, 0.03, 1 / 3))
    first = tiered_take_profit_action(10, 12, 11.4, tiers)
    assert first == ("take_profit_trailing_0.2_0.05", 1 / 3, "take_profit_trailing_0.2_0.05")

    fired = {first[2]}
    assert tiered_take_profit_action(10, 12, 11.4, tiers, fired_tiers=fired) is None
    second = tiered_take_profit_action(10, 13, 12.6, tiers, fired_tiers=fired)
    assert second == ("take_profit_trailing_0.3_0.03", 1 / 3, "take_profit_trailing_0.3_0.03")


def test_atr_percent_uses_true_range_and_warms_up():
    history = deque(maxlen=2)
    previous, atr = _update_atr_percent(
        {"high": 11.0, "low": 9.0, "close": 10.0}, None, history, 2,
    )
    assert previous == 10.0
    assert atr is None
    previous, atr = _update_atr_percent(
        {"high": 10.5, "low": 9.5, "close": 10.0}, previous, history, 2,
    )
    assert previous == 10.0
    assert atr == pytest.approx(0.15)


def test_atr_take_profit_uses_stable_stage_ids_and_partial_fractions():
    tiers = ((0.20, 2.0, 1 / 3), (0.30, 1.25, 1 / 3))
    first = atr_take_profit_action(10, 12, 11.4, 0.025, tiers)
    assert first == ("take_profit_atr_0.2_2", 1 / 3, "take_profit_atr_0.2_2")

    fired = {first[2]}
    assert atr_take_profit_action(10, 12, 11.4, 0.02, tiers, fired_tiers=fired) is None
    second = atr_take_profit_action(10, 13, 12.6, 0.02, tiers, fired_tiers=fired)
    assert second == ("take_profit_atr_0.3_1.25", 1 / 3, "take_profit_atr_0.3_1.25")
