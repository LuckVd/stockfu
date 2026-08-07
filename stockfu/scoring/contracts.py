"""V2 评分核心数据契约。

严格对应 docs/SPECS/factor-strategy-score-v2.md §5.1–5.3。

设计要点:
- 原始值缺失时 raw_value 必须为 None,禁止用 0 顶替(§5.1 末)。
- 所有数值内部双精度;UI 才四舍五入(§5.3 末)。
- profile/alpha/policy/risk 均不可变,改动创建新版本(§7、§19)。
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any


# --------------------------------------------------------------------- 枚举


class Direction(str, Enum):
    """因子方向。所有 factor_score 最终都统一为「越高越好」。"""

    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"


class Maturity(str, Enum):
    """历史成熟状态(§5.2)。"""

    IMMATURE = "immature"   # 关键样本严重不足,等价无证据
    PARTIAL = "partial"     # 部分分量未成熟
    MATURE = "mature"       # 全部分量成熟


class ScoreStatus(str, Enum):
    """策略可交易状态(§10.1)。展示可输出数值,但 not_tradable 不得下单。"""

    TRADABLE = "tradable"
    NOT_TRADABLE = "not_tradable"      # 关键因子缺失或覆盖不足
    OBSERVATION = "observation"         # 处于观察期,不交易
    NO_UNIVERSE = "no_universe"         # 不在合格股票池


class MissingReason(str, Enum):
    """原始值缺失原因(§5.1)。"""

    INSUFFICIENT_SAMPLES = "insufficient_samples"
    FIELD_MISSING = "field_missing"
    NONPOSITIVE_DENOMINATOR = "nonpositive_denominator"
    NOT_DISCLOSED = "not_disclosed"
    NONTRADING = "nontrading"


class MappingMode(str, Enum):
    """映射模式(§6)。"""

    FIXED = "fixed"      # 固定锚点等比例
    HYBRID = "hybrid"    # 绝对锚点 + 多历史分量加权


# ---------------------------------------------------------- canonical 指纹


def _jsonable(obj: Any) -> Any:
    """递归把对象转成可稳定序列化的形式(键排序、浮点规范化、日期 ISO)。"""
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, float):
        if math.isnan(obj):
            return "NaN"
        if math.isinf(obj):
            return "Infinity" if obj > 0 else "-Infinity"
        # 规范化浮点:截到 1e-12 精度避免表示漂移,再 round-trip 去尾零。
        return round(obj, 12)
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    # dataclass / 任意对象:走 asdict 或 __dict__
    if hasattr(obj, "__dataclass_fields__"):
        return _jsonable(asdict(obj))
    if hasattr(obj, "__dict__"):
        return _jsonable(obj.__dict__)
    return str(obj)


def canonical_json(obj: Any) -> str:
    """稳定的 canonical JSON:键排序 + 浮点/日期规范化(§21 阶段1)。"""
    return json.dumps(_jsonable(obj), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def fingerprint(obj: Any, *, prefix: str = "") -> str:
    """sha256 指纹(十六进制)。profile/alpha/policy/raw 算法版本均用此。"""
    h = hashlib.sha256()
    if prefix:
        h.update(prefix.encode("utf-8"))
        h.update(b"|")
    h.update(canonical_json(obj).encode("utf-8"))
    return h.hexdigest()


# ----------------------------------------------------------- §5.1 原始观测


@dataclass
class RawFactorObservation:
    """单个证券单个交易日的单个原始指标观测(§5.1)。"""

    asset_code: str
    as_of: date
    raw_metric_id: str
    raw_value: float | None
    raw_unit: str
    source_max_date: date          # 参与计算的数据最大日期,必须 <= as_of
    available_at: date             # 现实中可获得时间
    valid: bool
    raw_fingerprint: str
    lookback_observations: int = 0
    missing_reason: MissingReason | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, date, str]:
        """自然键:(code, as_of, raw_metric_id)。"""
        return (self.asset_code, self.as_of, self.raw_metric_id)


# -------------------------------------------------------- §5.2 因子分观测


@dataclass
class FactorScoreObservation:
    """一个原始值经 profile 映射后的 0–100 因子分(§5.2)。"""

    profile_id: str
    profile_version: int
    asset_code: str
    as_of: date
    raw_metric_id: str
    score: float                        # 最终 0–100 因子分
    evidence_coverage: float            # 有效映射权重占比 0–1
    maturity: Maturity
    mapping_fingerprint: str
    reference_cutoff: date | None       # 所用历史状态最大日期,正常 = 上一交易日
    absolute_score: float | None = None
    market_history_score: float | None = None
    industry_history_score: float | None = None
    self_history_score: float | None = None
    raw_value: float | None = None
    history_n: dict[str, int] = field(default_factory=dict)
    state_hash: str = ""
    warnings: list[str] = field(default_factory=list)
    missing_reason: MissingReason | None = None
    raw_fingerprint: str = ""
    formal_requires_mature: bool = True


# ------------------------------------------------------ §5.3 策略分观测


@dataclass
class StrategyScoreObservation:
    """一只股票一天对一个 alpha 的策略评分(§5.3)。"""

    alpha_id: str
    alpha_version: int
    asset_code: str
    as_of: date
    strategy_score: float               # 直接用于展示/选股,禁止再映射
    effective_coverage: float
    score_status: ScoreStatus
    alpha_fingerprint: str
    mapping_fingerprints: dict[str, str]        # profile_id -> mapping fingerprint
    configured_weights: dict[str, float]        # profile_id -> weight
    factor_scores: dict[str, FactorScoreObservation] = field(default_factory=dict)
    universe_status: str = "in_universe"
    reference_cutoff: date | None = None
    reasons: list[str] = field(default_factory=list)
