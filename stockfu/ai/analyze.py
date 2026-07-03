"""AI 分析入口:取数 → run_with_tools(4 顾问,含工具循环) → 汇总 → 润色"""
from __future__ import annotations

import json

from stockfu.ai.client import chat_completion
from stockfu.ai.context import build_context
from stockfu.ai.skills.advisors import ALL_ADVISORS
from stockfu.ai.skills.advisors.base import AdvisorContext, Opinion
from stockfu.ai.synthesis import aggregate, narrate
from stockfu.ai.skills.tools import (discover_and_register, get_tools_for,
                                     get_tool_descriptions_for, execute_tool,
                                     clear_log)


def run_with_tools(advisor_cls, ctx: AdvisorContext) -> Opinion:
    """顾问分析(带工具循环): prompt → LLM(tool_calls) → 执行 → 回传 → 最终结果。"""
    a = advisor_cls()
    tools = get_tools_for(a.advisor_id)

    # 拼接 system prompt + 工具描述
    system = a.system_prompt()
    desc = get_tool_descriptions_for(a.advisor_id)
    if desc:
        system += "\n\n" + desc

    user = a.build_user_message(ctx)
    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    tools_used_records: list[dict] = []
    first_round = True

    for _ in range(5):  # max 5 轮(防死循环)
        resp = chat_completion(messages, tools=tools if first_round else None, temperature=0.2)
        first_round = False
        choice = resp["choices"][0]
        finish = choice["finish_reason"]

        if finish == "tool_calls":
            msg = choice["message"]
            messages.append({
                "role": "assistant",
                "content": msg.get("content", ""),
                "tool_calls": msg["tool_calls"],
            })
            for tc in msg["tool_calls"]:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except (json.JSONDecodeError, TypeError):
                    args = {}
                result = execute_tool(name, code=ctx.code, **args)
                tools_used_records.append({"tool": name, "args": args, "result": result})
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
        else:
            content = choice["message"].get("content", "")
            # reasoning 模型在工具调用后可能 content 为空(推理已够但没输出文本)
            # → 追加一轮要求输出 JSON,并用 json_repair 容错
            if not content and tools_used_records:
                messages.append({
                    "role": "user",
                    "content": (
                        '根据以上数据与工具结果,输出你的最终意见。'
                        '只输出一个合法的JSON对象,禁止markdown代码块,禁止额外文字:'
                        '{"signal":"buy","score_adjustment":0,"confidence":0.5,"reasoning":"...","evidence":{}}'
                    ),
                })
                resp = chat_completion(messages, temperature=0.2)
                raw = resp["choices"][0]["message"].get("content", "")
                if raw:
                    from json_repair import repair_json
                    try:
                        content = repair_json(raw)
                    except Exception:  # noqa: BLE001
                        content = "{}"
                else:
                    content = "{}"
            try:
                if not content:
                    content = "{}"
                op = a.parse(ctx, content)
            except Exception:  # noqa: BLE001
                op = Opinion(
                    advisor=a.advisor_id, signal="hold",
                    score_adjustment=0, confidence=0.0,
                    reasoning=f"[格式化输出失败] {choice['finish_reason']}",
                )
            op.tools_used = tools_used_records
            return op

    return Opinion(
        advisor=a.advisor_id, signal="hold",
        score_adjustment=0, confidence=0.0,
        reasoning=f"[工具调用超限] 最终: {messages[-1].get('content', '')[:200]}",
    )


def analyze(code: str) -> dict:
    """对单只股票跑完整 4 顾问分析。返回 {context, opinions, aggregate, narrative}。"""
    discover_and_register()
    clear_log()

    ctx = build_context(code)
    opinions: list[Opinion] = []
    for advisor_cls in ALL_ADVISORS:
        try:
            opinions.append(run_with_tools(advisor_cls, ctx))
        except Exception as exc:  # noqa: BLE001
            opinions.append(Opinion(
                advisor=advisor_cls.advisor_id, signal="hold",
                score_adjustment=0, confidence=0.0,
                reasoning=f"[顾问调用失败] {type(exc).__name__}: {exc}",
            ))

    agg = aggregate(opinions)
    try:
        report = narrate(agg)
    except Exception as exc:  # noqa: BLE001
        report = f"[综合解读生成失败] {exc}"

    return {
        "code": code,
        "name": ctx.name,
        "context": ctx.__dict__,
        "opinions": agg["opinions"],
        "aggregate": {k: v for k, v in agg.items() if k != "opinions"},
        "narrative": report,
    }
