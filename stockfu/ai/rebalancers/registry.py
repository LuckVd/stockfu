"""仓位调整方案注册表:装饰器注册 → 目录扫描发现 → 按 id 取类 + active 指针。

照搬 operators/registry.py 骨架,独立 REGISTRY(rebalancer 不进算子表/不缓存,语义不同)。
active 由 app_config('active_rebalancer_id') 单 key 指针决定(复刻 active_strategy_id)。
"""
from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path
from typing import Type

from stockfu.ai.rebalancers.base import Rebalancer

log = logging.getLogger(__name__)

REGISTRY: dict[str, Type[Rebalancer]] = {}


def register(cls: Type[Rebalancer]) -> Type[Rebalancer]:
    """装饰器:注册调整方案类。用法 @register。重复注册后者覆盖前者(便于热重载)。"""
    rid = getattr(cls, "rebalancer_id", "")
    if not rid:
        raise ValueError(f"{cls.__name__} 缺 rebalancer_id 属性")
    REGISTRY[rid] = cls
    return cls


def discover_and_register() -> None:
    """自动扫描 rebalancers/*.py(单层平铺),触发模块顶层 @register。

    单层不分子目录(方案数量少,与 aggregators/ 一层平铺一致)。
    """
    pkg_dir = Path(__file__).parent
    for f in sorted(pkg_dir.glob("*.py")):
        if f.stem in ("__init__", "base", "registry"):
            continue
        importlib.import_module(f"stockfu.ai.rebalancers.{f.stem}")


def get_rebalancer(rebalancer_id: str) -> Type[Rebalancer] | None:
    return REGISTRY.get(rebalancer_id)


def get_rebalancer_params() -> dict:
    """读 app_config('rebalancer_params') JSON → dict(失败/空 → {})。"""
    from stockfu.db import get_app_config
    try:
        return json.loads(get_app_config("rebalancer_params", "{}") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def get_active_rebalancer() -> Rebalancer:
    """读 app_config('active_rebalancer_id') 指针 → 实例化。

    无指针/未知 id → PassThrough(fallback + warning,可插拔不崩)。复刻 get_active_strategy。
    切方案:set_app_config('active_rebalancer_id', 'cap_and_rank')。
    """
    from stockfu.db import get_app_config
    if not REGISTRY:
        discover_and_register()
    rid = get_app_config("active_rebalancer_id", "pass_through")
    cls = REGISTRY.get(rid)
    if cls is None:
        log.warning("未知 rebalancer '%s',回退 pass_through", rid)
        from stockfu.ai.rebalancers.pass_through import PassThrough
        return PassThrough()
    return cls()
