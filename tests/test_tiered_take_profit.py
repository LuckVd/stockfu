from stockfu.backtest.engine import tiered_take_profit_action, tiered_take_profit_reason


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
