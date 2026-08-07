from datetime import date

import pytest


def test_v1_scheduler_is_disabled():
    from stockfu.backtest.scheduler import run

    with pytest.raises(RuntimeError, match="V1 回测引擎已禁用"):
        run([], date(2024, 1, 1), date(2024, 1, 2))


def test_v1_engine_is_disabled():
    from stockfu.backtest.engine import run_backtest

    with pytest.raises(RuntimeError, match="V1 回测引擎已禁用"):
        run_backtest([], date(2024, 1, 1), date(2024, 1, 2))
