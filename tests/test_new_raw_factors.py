"""新接入量价微观结构与财报事件 raw 因子回归。

这些测试不访问真实数据库，使用小型行情/财报序列验证计算口径、缺失语义、PIT
可用时间和参数进入 raw fingerprint，防止研究脚本与回测计算器悄悄分叉。
"""
from datetime import date, timedelta
from unittest.mock import patch

import pytest

from stockfu.factors.raw.earnings_event import (
    compute_jor,
    compute_rec_acc_rev,
    compute_sue_rw,
)
from stockfu.factors.raw.price_micro import (
    compute_amihud,
    compute_cgo,
    compute_intraday_ret,
    compute_overnight_ret,
    compute_wsplit_rev,
)
from stockfu.scoring.contracts import MissingReason


AS_OF = date(2024, 1, 31)


def _dates(n: int) -> list[date]:
    start = date(2024, 1, 1)
    return [start + timedelta(days=i) for i in range(n)]


def test_overnight_and_intraday_use_raw_components():
    dates = _dates(21)
    closes = [100.0]
    for _ in range(20):
        closes.append(closes[-1] * 1.03)
    opens = [100.0] + [closes[i - 1] * 1.01 for i in range(1, 21)]

    def bars(_code, field, _days, as_of=None, adj="qfq"):
        assert adj == "raw"
        return dates, opens if field == "open" else closes

    with patch("stockfu.services.factors.quote_series_dates", side_effect=bars):
        overnight = compute_overnight_ret("600001", AS_OF)
        intraday = compute_intraday_ret("600001", AS_OF)

    assert overnight.valid is True
    assert overnight.raw_value == pytest.approx(1.0)
    assert overnight.lookback_observations == 20
    assert intraday.valid is True
    assert intraday.raw_value == pytest.approx(2.0)
    assert intraday.lookback_observations == 20


def test_cgo_amihud_and_wsplit_calculations():
    flat_closes = [100.0] * 20

    def flat_series(_code, field, _days, as_of=None, adj="qfq"):
        if field == "close":
            return flat_closes
        return [10.0] * 20

    with patch("stockfu.services.factors.quote_series", side_effect=flat_series):
        cgo = compute_cgo("600001", AS_OF)

    assert cgo.valid is True
    assert cgo.raw_value == pytest.approx(0.0)
    assert cgo.diagnostics["rp"] == pytest.approx(100.0)

    closes = [100.0 * (1.01 ** i) for i in range(21)]

    def amihud_series(_code, field, _days, as_of=None, adj="qfq"):
        if field == "close":
            return closes
        return [1e8] * 21

    with patch("stockfu.services.factors.quote_series", side_effect=amihud_series):
        amihud = compute_amihud("600001", AS_OF)

    assert amihud.valid is True
    assert amihud.raw_value == pytest.approx(10.0)
    assert amihud.raw_unit == "per_1e8_cny_x1000"

    closes = [100.0]
    for i in range(1, 21):
        closes.append(closes[-1] * (1.02 if i <= 10 else 0.99))
    volumes = [1.0] * 21
    amounts = [2e8 if i <= 10 else 1e8 for i in range(21)]

    def wsplit_series(_code, field, _days, as_of=None, adj="qfq"):
        if field == "close":
            return closes
        if field == "volume":
            return volumes
        return amounts

    with patch("stockfu.services.factors.quote_series", side_effect=wsplit_series):
        wsplit = compute_wsplit_rev("600001", AS_OF)

    assert wsplit.valid is True
    assert wsplit.raw_value == pytest.approx(30.0)


def test_price_micro_insufficient_samples_is_explicit():
    def short_series(_code, _field, _days, as_of=None, adj="qfq"):
        return [100.0] * 10

    with patch("stockfu.services.factors.quote_series", side_effect=short_series):
        obs = compute_amihud("600001", AS_OF)

    assert obs.valid is False
    assert obs.raw_value is None
    assert obs.missing_reason == MissingReason.INSUFFICIENT_SAMPLES


def test_sue_is_pit_and_lookback_is_in_fingerprint():
    ttms = [
        (date(2022, 1, 1), 100.0),
        (date(2022, 4, 1), 101.0),
        (date(2022, 7, 1), 103.0),
        (date(2022, 10, 1), 102.0),
        (date(2023, 1, 1), 106.0),
        (date(2023, 4, 1), 110.0),
    ]

    with patch("stockfu.factors.raw.earnings_event._ttm_series", return_value=ttms):
        obs4 = compute_sue_rw("600001", AS_OF, lookback=4)
        obs5 = compute_sue_rw("600001", AS_OF, lookback=5)

    assert obs4.valid is True
    assert obs4.available_at == date(2023, 4, 1)
    assert obs4.source_max_date == date(2023, 4, 1)
    assert obs4.raw_fingerprint != obs5.raw_fingerprint


def test_event_factors_use_pub_date_and_parameterized_event_window():
    dates = _dates(6)
    opens = [100.0, 105.0, 105.0, 105.0, 105.0, 105.0]
    closes = [100.0, 95.0, 94.0, 93.0, 92.0, 90.0]

    quote_rows = tuple(
        (d, opening, closing, closing)
        for d, opening, closing in zip(dates, opens, closes)
    )

    with patch("stockfu.factors.raw.earnings_event._event_day", return_value=dates[0]), \
         patch("stockfu.factors.raw.earnings_event._quote_rows", return_value=quote_rows):
        jor = compute_jor("600001", dates[-1], hold_days=5)
        stale = compute_jor("600001", dates[-1], hold_days=3)
        rec_acc = compute_rec_acc_rev("600001", dates[-1], hold_days=5)

    assert jor.valid is True
    assert jor.raw_value == pytest.approx(5.0)
    assert jor.available_at == dates[0]
    assert stale.valid is False
    assert stale.missing_reason == MissingReason.INSUFFICIENT_SAMPLES
    assert jor.raw_fingerprint != stale.raw_fingerprint
    assert rec_acc.valid is True
    assert rec_acc.raw_value == pytest.approx(-(90.0 / 95.0 - 1.0) * 100.0)
    assert rec_acc.available_at == dates[0]
