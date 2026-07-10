"""算子注册表: 装饰器注册 → 目录扫描发现 → 按 id 取类。

复用 skills/tools/__init__.py 的 importlib 扫描骨架,扫 operators/{factors,llm,aggregators}/。
每个算子模块在顶层用 @register 装饰算子类即可被自动发现注册。
"""
from __future__ import annotations

import importlib
from pathlib import Path
from typing import Type

from stockfu.ai.operators.base import BaseOperator

REGISTRY: dict[str, Type[BaseOperator]] = {}


def register(cls: Type[BaseOperator]) -> Type[BaseOperator]:
    """装饰器:注册算子类。用法 @register。重复注册后者覆盖前者(便于热重载)。"""
    if not getattr(cls, "operator_id", ""):
        raise ValueError(f"{cls.__name__} 缺 operator_id 属性")
    REGISTRY[cls.operator_id] = cls
    return cls


def discover_and_register() -> None:
    """自动扫描 operators/{factors,llm,aggregators}/*.py,触发模块顶层 @register。

    子目录不存在则跳过(便于分阶段交付:首阶段即使某类目录未建也不崩)。
    """
    pkg_dir = Path(__file__).parent
    for sub in ("factors", "llm", "aggregators"):
        sub_dir = pkg_dir / sub
        if not sub_dir.is_dir():
            continue
        for f in sorted(sub_dir.glob("*.py")):
            if f.stem == "__init__":
                continue
            importlib.import_module(f"stockfu.ai.operators.{sub}.{f.stem}")


def get_operator_class(operator_id: str) -> Type[BaseOperator] | None:
    return REGISTRY.get(operator_id)


def all_operators() -> dict[str, Type[BaseOperator]]:
    return dict(REGISTRY)
