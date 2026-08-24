"""负盈利/负净资产语义(spec §11.1):负值是有效负证据,仅 None/0 才缺失。

2026-08-24 审查修复:earnings_yield/book_to_price 曾把 PE/PB<0 当缺失,
亏损股价值腿收缩到中性 50 → 价值类荐股排名系统性虚高。
"""
from datetime import date

from unittest.mock import patch

from stockfu.factors.raw.book_to_price import compute_book_to_price
from stockfu.factors.raw.earnings_yield import compute_earnings_yield
from stockfu.scoring.contracts import MissingReason
from stockfu.scoring.mappings import fixed_score

AS_OF = date(2024, 6, 3)


def _patch_valuation(pe=None, pb=None):
    return patch("stockfu.services.valuation.pe_pb_at", return_value=(pe, pb))


def test_negative_pe_yields_negative_valid_earnings_yield():
    """PE<0(亏损)→ 负 E/P,valid,不再当缺失。"""
    with _patch_valuation(pe=-20.0):
        obs = compute_earnings_yield("600001", AS_OF)
    assert obs.valid is True
    assert obs.raw_value == -5.0
    assert obs.missing_reason is None


def test_zero_or_missing_pe_stays_missing():
    """PE=0/None(数据缺失/异常)才按缺失。"""
    with _patch_valuation(pe=0.0):
        obs = compute_earnings_yield("600001", AS_OF)
    assert obs.valid is False
    assert obs.raw_value is None
    assert obs.missing_reason == MissingReason.NONPOSITIVE_DENOMINATOR

    with _patch_valuation(pe=None):
        obs = compute_earnings_yield("600001", AS_OF)
    assert obs.valid is False


def test_negative_pb_yields_negative_valid_book_to_price():
    """PB<0(负净资产)→ 负 B/P,valid。"""
    with _patch_valuation(pb=-2.0):
        obs = compute_book_to_price("600001", AS_OF)
    assert obs.valid is True
    assert obs.raw_value == -0.5
    assert obs.missing_reason is None


def test_zero_or_missing_pb_stays_missing():
    with _patch_valuation(pb=0.0):
        obs = compute_book_to_price("600001", AS_OF)
    assert obs.valid is False
    assert obs.missing_reason == MissingReason.NONPOSITIVE_DENOMINATOR

    with _patch_valuation(pb=None):
        obs = compute_book_to_price("600001", AS_OF)
    assert obs.valid is False


def test_profile_knots_map_negative_evidence_to_floor():
    """profile 负锚点:亏损 -5% 映射到 0 分附近,而非中性 50。"""
    knots_ey = [[-10, 0], [0, 0], [2, 30], [4, 45], [6, 55], [8, 65], [12, 80], [20, 100]]
    knots_bp = [[-0.5, 0], [0, 0], [0.2, 30], [0.4, 45], [0.6, 55],
                [0.8, 65], [1.0, 72], [2.0, 90], [3.0, 100]]
    assert fixed_score(-5.0, knots_ey) <= 1.0      # 亏损贴地
    assert fixed_score(0.0, knots_ey) == 0.0
    assert fixed_score(8.0, knots_ey) == 65.0      # 正常段不受影响
    assert fixed_score(-0.3, knots_bp) <= 1.0
    assert fixed_score(1.0, knots_bp) == 72.0
