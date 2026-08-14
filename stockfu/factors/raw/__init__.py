"""V2 纯原始因子计算器(stockfu/factors/raw/)。

每个计算器只返回 RawFactorObservation(原始值 + source_max_date + 指纹),
不输出 score、不读历史分位(那是 scoring 层职责)。
点时正确性继承 services 层的 quote_series / dividend_yield_ttm(已防未来)。
"""
from __future__ import annotations

from typing import Any

from stockfu.scoring.contracts import fingerprint


def raw_fingerprint(metric_id: str, algo: str, params: dict[str, Any]) -> str:
    """raw 算法指纹:metric + 算法 + 参数 + 口径 + 版本。改动 → 新指纹 → 旧缓存失效。"""
    return fingerprint(
        {"metric": metric_id, "algo": algo, "params": params, "schema": "v2-raw-1"},
        prefix="raw",
    )
