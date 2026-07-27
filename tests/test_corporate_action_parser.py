from datetime import date

import pandas as pd

from stockfu.data.dividend_parser import extract_corporate_actions_from_history


def test_mixed_distribution_uses_pre_event_share_basis():
    rows = pd.DataFrame([{
        "进度": "实施", "除权除息日": "2010-04-16", "股权登记日": "2010-04-15",
        "公告日期": "2010-04-10", "派息": 1.0, "送股": 2.0, "转增": 10.0,
    }])

    events = extract_corporate_actions_from_history(rows, "300024")

    assert events == [
        type(events[0])(
            ex_date=date(2010, 4, 16), per_share_cash=0.1, per_share_stock=1.2,
            record_date=date(2010, 4, 15), announce_date=date(2010, 4, 10),
            currency="CNY", source="akshare:stock_history_dividend_detail",
        )
    ]


def test_unimplemented_or_empty_rows_are_not_account_events():
    rows = pd.DataFrame([
        {"进度": "预案", "除权除息日": "2010-04-16", "派息": 1.0},
        {"进度": "实施", "除权除息日": None, "派息": 1.0},
        {"进度": "实施", "除权除息日": "2010-05-01", "派息": 0, "送股": 0, "转增": 0},
    ])

    assert extract_corporate_actions_from_history(rows, "300024") == []
