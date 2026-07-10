"""工具注册表: 发现 → 按顾问筛选 → 执行 → 调用记录(operators/llm 镜像,逐字复制自 skills/tools/)。"""
from __future__ import annotations

import importlib
from pathlib import Path

REGISTRY: dict[str, dict] = {}
TOOL_CALL_LOG: list[dict] = []


def register(name: str, schema: dict, execute_fn, used_by: set, required_fields: list[str] | None = None) -> None:
    REGISTRY[name] = {
        "schema": schema, "execute": execute_fn,
        "used_by": set(used_by),
        "required_fields": required_fields or [],
    }


def discover_and_register() -> None:
    """自动扫描 tools/*.py,注册每个模块的 SCHEMA/execute/USED_BY。"""
    tools_dir = Path(__file__).parent
    for f in sorted(tools_dir.glob("*.py")):
        if f.stem == "__init__":
            continue
        mod = importlib.import_module(f"stockfu.ai.operators.llm.tools.{f.stem}")
        if hasattr(mod, "SCHEMA") and hasattr(mod, "execute") and hasattr(mod, "USED_BY"):
            register(
                name=mod.SCHEMA["function"]["name"],
                schema=mod.SCHEMA,
                execute_fn=mod.execute,
                used_by=mod.USED_BY,
                required_fields=getattr(mod, "REQUIRED_FIELDS", []),
            )


def get_tools_for(advisor_id: str) -> list[dict]:
    """返回该顾问可见的 tools schema 列表(OpenAI function calling 格式)。"""
    return [r["schema"] for r in REGISTRY.values() if advisor_id in r["used_by"]]


def get_tool_descriptions_for(advisor_id: str) -> str:
    """返回该顾问可见工具的文本描述,嵌入 prompt。"""
    lines = ["## 可用分析工具\n"]
    for r in REGISTRY.values():
        if advisor_id in r["used_by"]:
            fn = r["schema"]["function"]
            lines.append(f"- {fn['name']}: {fn['description']}")
    return "\n".join(lines) if len(lines) > 1 else ""


def execute_tool(name: str, code: str, as_of=None, **kwargs) -> str:
    """执行工具并记录调用。返回人类可读结果。

    as_of 透传给 tool.execute → quote_series(回测防未来函数上界);LLM 参数走 kwargs。
    """
    tool = REGISTRY.get(name)
    if not tool:
        return f"[错误:未知工具 '{name}']"
    result = tool["execute"](code, as_of=as_of, **kwargs)
    TOOL_CALL_LOG.append({"tool": name, "args": kwargs, "result": result})
    return result


def clear_log() -> None:
    TOOL_CALL_LOG.clear()


def get_all_required_fields() -> list[str]:
    """汇总所有工具所需的数据字段,用于记录数据缺口。"""
    seen: set[str] = set()
    for r in REGISTRY.values():
        seen.update(r["required_fields"])
    return sorted(seen)
