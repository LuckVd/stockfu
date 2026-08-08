"""因子评分档案(profile):不可变配置 + 版本 + 指纹(设计 §7)。

档案把「原始值 → 0–100」的映射规则冻结。策略只引用 profile_id;相同原始算法
但不同窗口/锚点是不同 profile(如 momentum_20d_v1 与 momentum_120d_v1)。

校验(§7 末):权重和=1、锚点单调、窗口为正、min_observations 非负、单位一致。
任何值改动 → 新 profile_id + version,不得覆盖旧版本。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from stockfu.scoring.contracts import (
    Direction,
    MappingMode,
    fingerprint,
)
from stockfu.scoring.mappings import Knots


HISTORY_COMPONENTS = ("market_history", "industry_history", "self_history")
SAMPLE_MONTH_END = "month_end"
SAMPLE_MONTH_END_CROSS = "month_end_cross_section"
SAMPLE_WEEKEND_CROSS = "weekend_cross_section"
SAMPLE_DAILY = "daily"
SAMPLING_MODES = {
    SAMPLE_MONTH_END, SAMPLE_MONTH_END_CROSS, SAMPLE_WEEKEND_CROSS, SAMPLE_DAILY,
}


@dataclass(frozen=True)
class HistorySpec:
    """单个历史分量的窗口与采样规格。"""

    weight: float
    state: str                      # rolling | expanding
    years: float
    sampling: str                   # month_end | month_end_cross_section | weekend_cross_section | daily
    min_observations: int

    def to_dict(self) -> dict[str, Any]:
        return {"weight": self.weight, "state": self.state, "years": self.years,
                "sampling": self.sampling, "min_observations": self.min_observations}


@dataclass(frozen=True)
class FactorProfile:
    """不可变因子评分档案。"""

    profile_id: str
    version: int
    raw_metric_id: str
    raw_metric_params: dict[str, Any]
    direction: Direction
    mode: MappingMode
    raw_unit: str
    absolute_weight: float
    absolute_knots: Knots | None
    history_specs: dict[str, HistorySpec]      # name -> spec (name in HISTORY_COMPONENTS)
    formal_requires_mature: bool = True
    missing_policy: str = "shrink_to_50_and_block_if_critical"
    valid_from: date = date(2007, 1, 1)

    # ---- 权重视图(供 mappings.combine_hybrid 使用)----

    def weights(self) -> dict[str, float]:
        w = {"absolute": self.absolute_weight}
        for name, spec in self.history_specs.items():
            w[name] = spec.weight
        return w

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "version": self.version,
            "raw_metric": {"id": self.raw_metric_id, "params": dict(self.raw_metric_params)},
            "direction": self.direction.value,
            "raw_unit": self.raw_unit,
            "mapping": {
                "mode": self.mode.value,
                "components": {
                    "absolute": {"weight": self.absolute_weight,
                                 "knots": [list(k) for k in self.absolute_knots]}
                    if self.absolute_knots is not None else {"weight": self.absolute_weight},
                    **{n: s.to_dict() for n, s in self.history_specs.items()},
                },
            },
            "formal_requires_mature": self.formal_requires_mature,
            "missing_policy": self.missing_policy,
            "valid_from": self.valid_from.isoformat(),
        }

    def mapping_fingerprint(self) -> str:
        """完整评分配置指纹(不含 raw 算法本身,只含映射)。"""
        return fingerprint(self.to_dict(), prefix="profile.mapping")


# ------------------------------------------------------------------- 校验


def _check_knots_monotone(knots: Knots, direction: Direction, profile_id: str) -> None:
    xs = [k[0] for k in knots]
    ss = [k[1] for k in knots]
    for i in range(len(xs) - 1):
        if not (xs[i] < xs[i + 1]):
            raise ValueError(f"profile {profile_id}: knots 必须按 raw 严格升序")
    if direction == Direction.HIGHER_IS_BETTER:
        for i in range(len(ss) - 1):
            if ss[i] > ss[i + 1]:
                raise ValueError(f"profile {profile_id}: higher_is_better 但 knots score 非单调递增")
    else:
        for i in range(len(ss) - 1):
            if ss[i] < ss[i + 1]:
                raise ValueError(f"profile {profile_id}: lower_is_better 但 knots score 非单调递减")


def validate_profile(p: FactorProfile) -> None:
    """加载时强校验(§7 末)。失败抛 ValueError。"""
    if p.version < 1:
        raise ValueError(f"profile {p.profile_id}: version>=1")
    total = p.absolute_weight + sum(s.weight for s in p.history_specs.values())
    if abs(total - 1.0) > 1e-9:
        raise ValueError(f"profile {p.profile_id}: 权重和={total}≠1")
    if p.absolute_weight < 0 or any(s.weight < 0 for s in p.history_specs.values()):
        raise ValueError(f"profile {p.profile_id}: 权重不得为负")
    if p.absolute_weight > 0:
        if not p.absolute_knots or len(p.absolute_knots) < 2:
            raise ValueError(f"profile {p.profile_id}: absolute 权重>0 需≥2 个 knots")
        _check_knots_monotone(p.absolute_knots, p.direction, p.profile_id)
    for name, spec in p.history_specs.items():
        if name not in HISTORY_COMPONENTS:
            raise ValueError(f"profile {p.profile_id}: 未知历史分量 {name}")
        if spec.years <= 0:
            raise ValueError(f"profile {p.profile_id}: {name}.years>0")
        if spec.min_observations < 0:
            raise ValueError(f"profile {p.profile_id}: {name}.min_observations>=0")
        if spec.state not in ("rolling", "expanding"):
            raise ValueError(f"profile {p.profile_id}: {name}.state 需 rolling|expanding")
        if spec.sampling not in SAMPLING_MODES:
            raise ValueError(f"profile {p.profile_id}: {name}.sampling 未知: {spec.sampling}")


# ------------------------------------------------------------------- 加载


def _parse_knots(raw: Any, profile_id: str) -> Knots:
    out: Knots = []
    for k in raw or []:
        if len(k) != 2:
            raise ValueError(f"profile {profile_id}: knot 需 [raw, score]")
        out.append((float(k[0]), float(k[1])))
    return out


def profile_from_dict(d: dict[str, Any]) -> FactorProfile:
    """从已解析的 dict 构造并校验 FactorProfile。"""
    pid = d["profile_id"]
    rm = d["raw_metric"]
    mapping = d["mapping"]
    components = mapping.get("components", {}) or {}
    absolute = components.get("absolute", {}) or {}
    abs_w = float(absolute.get("weight", 0.0))
    abs_knots = _parse_knots(absolute.get("knots"), pid) if abs_w > 0 else None

    history_specs: dict[str, HistorySpec] = {}
    for name in HISTORY_COMPONENTS:
        c = components.get(name)
        if not c:
            continue
        history_specs[name] = HistorySpec(
            weight=float(c["weight"]),
            state=str(c.get("state", "rolling")),
            years=float(c.get("years", 3)),
            sampling=str(c.get("sampling", SAMPLE_MONTH_END)),
            min_observations=int(c.get("min_observations", 0)),
        )

    p = FactorProfile(
        profile_id=pid,
        version=int(d["version"]),
        raw_metric_id=rm["id"],
        raw_metric_params=dict(rm.get("params", {}) or {}),
        direction=Direction(d["direction"]),
        mode=MappingMode(mapping["mode"]),
        raw_unit=str(d.get("raw_unit", _unit_from_metric(rm["id"]))),
        absolute_weight=abs_w,
        absolute_knots=abs_knots,
        history_specs=history_specs,
        formal_requires_mature=bool(d.get("formal_requires_mature", True)),
        missing_policy=str(d.get("missing_policy", "shrink_to_50_and_block_if_critical")),
        valid_from=date.fromisoformat(str(d.get("valid_from", "2007-01-01"))),
    )
    validate_profile(p)
    return p


def _unit_from_metric(metric_id: str) -> str:
    if "yield" in metric_id or "dividend" in metric_id:
        return "percent"
    if "volatility" in metric_id:
        return "annualized_vol"
    if "beta" in metric_id:
        return "ratio"
    return "ratio"


# ------------------------------------------------------------------- registry


class ProfileRegistry:
    """profile_id -> FactorProfile。加载时去重 + 指纹唯一。"""

    def __init__(self) -> None:
        self._by_id: dict[str, FactorProfile] = {}
        self._fingerprints: set[str] = set()

    def register(self, p: FactorProfile) -> FactorProfile:
        validate_profile(p)
        fp = p.mapping_fingerprint()
        existing = self._by_id.get(p.profile_id)
        if existing is not None and existing.mapping_fingerprint() != fp:
            raise ValueError(f"profile {p.profile_id} 已注册且指纹不同 → 需新 version")
        if fp in self._fingerprints and existing is None:
            raise ValueError(f"profile {p.profile_id} 与已有 profile 指纹重复")
        self._by_id[p.profile_id] = p
        self._fingerprints.add(fp)
        return p

    def get(self, profile_id: str) -> FactorProfile:
        if profile_id not in self._by_id:
            raise KeyError(f"未知 profile_id: {profile_id}")
        return self._by_id[profile_id]

    def all(self) -> dict[str, FactorProfile]:
        return dict(self._by_id)
