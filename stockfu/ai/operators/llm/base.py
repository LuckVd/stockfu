"""LLM 算子基类 + 工具循环(从 ai/analyze.py 迁入)。

迁移动机:让 LLM 算子的 system_prompt 可从 operator 表 DB 热改(prompt_override),
不重启生效;工具循环逻辑与原 analyze.run_with_tools 等价(含 json_repair 容错、
工具超限兜底),只是入参从 advisor_cls 改为 advisor 实例 + prompt_override。
"""
from __future__ import annotations

import json

from stockfu.ai.client import chat_completion
from stockfu.ai.operators.base import BaseOperator, OpContext, OpResult
from stockfu.ai.operators.llm.advisors.base import AdvisorContext, BaseAdvisor, Opinion
from stockfu.ai.operators.llm.tools import (execute_tool, get_tool_descriptions_for,
                                            get_tools_for)


def run_with_tools(advisor: BaseAdvisor, ctx: AdvisorContext,
                   temperature: float = 0.2,
                   prompt_override: str | None = None) -> Opinion:
    """顾问分析(带工具循环)。迁移自 ai/analyze.run_with_tools。

    advisor: 已实例化的顾问(提供 advisor_id/build_user_message/parse)。
    prompt_override: DB 来的 system_prompt;None→用 advisor.system_prompt()(兜底)。
    """
    tools = get_tools_for(advisor.advisor_id)
    system = prompt_override if prompt_override is not None else advisor.system_prompt()
    desc = get_tool_descriptions_for(advisor.advisor_id)
    if desc:
        system += "\n\n" + desc

    user = advisor.build_user_message(ctx)
    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    tools_used_records: list[dict] = []
    first_round = True

    for _ in range(5):  # max 5 轮(防死循环)
        resp = chat_completion(messages, tools=tools if first_round else None,
                               temperature=temperature)
        first_round = False
        choice = resp["choices"][0]
        finish = choice["finish_reason"]

        if finish == "tool_calls":
            msg = choice["message"]
            messages.append({
                "role": "assistant", "content": msg.get("content", ""),
                "tool_calls": msg["tool_calls"],
            })
            for tc in msg["tool_calls"]:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except (json.JSONDecodeError, TypeError):
                    args = {}
                result = execute_tool(name, code=ctx.code, as_of=ctx.as_of, **args)
                tools_used_records.append({"tool": name, "args": args, "result": result})
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
        else:
            content = choice["message"].get("content", "")
            if not content and tools_used_records:
                messages.append({
                    "role": "user",
                    "content": (
                        "根据以上数据与工具结果,输出你的最终意见。"
                        "只输出一个合法的JSON对象,禁止markdown代码块,禁止额外文字:"
                        '{"signal":"buy","score_adjustment":0,"confidence":0.5,'
                        '"reasoning":"...不超过500字","evidence":{}}'
                    ),
                })
                resp = chat_completion(messages, temperature=temperature)
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
                op = advisor.parse(ctx, content)
            except Exception:  # noqa: BLE001
                op = Opinion(
                    advisor=advisor.advisor_id, signal="hold",
                    score_adjustment=0, confidence=0.0,
                    reasoning=f"[格式化输出失败] {choice['finish_reason']}",
                )
            op.tools_used = tools_used_records
            return op

    return Opinion(
        advisor=advisor.advisor_id, signal="hold",
        score_adjustment=0, confidence=0.0,
        reasoning=f"[工具调用超限] 最终: {messages[-1].get('content', '')[:200]}",
    )


class LLMOperator(BaseOperator):
    """LLM 顾问算子基类。子类设 operator_id + advisor_cls。

    prompt: 从 operator 表 DB 加载的 system_prompt(None→用 advisor.system_prompt())。
        runner 实例化时传入,实现热改不重启。
    advisor_cls: 对应的 BaseAdvisor 子类(复用 build_user_message/parse/advisor_id)。
    """
    operator_id = ""
    type = "llm"
    advisor_cls: type[BaseAdvisor] | None = None

    def __init__(self, prompt: str | None = None):
        self.prompt = prompt

    def run(self, ctx: OpContext, params: dict) -> OpResult:
        from stockfu.ai.context import build_context

        # advisor_ctx 由 runner 预填充(混合作证共享);缺省现场 build(去持仓,不传 holding)
        advisor_ctx = ctx.advisor_ctx
        if advisor_ctx is None:
            advisor_ctx = build_context(ctx.code, as_of=ctx.as_of)

        if self.advisor_cls is None:
            return OpResult(operator=self.operator_id, type="llm",
                            reasoning=f"[LLM算子未绑 advisor_cls] {self.operator_id}",
                            confidence=0.0)

        advisor = self.advisor_cls()
        prompt = self.prompt if self.prompt is not None else advisor.system_prompt()
        temperature = params.get("temperature", 0.2)

        try:
            op = run_with_tools(advisor, advisor_ctx, temperature=temperature,
                                prompt_override=prompt)
        except Exception as exc:  # noqa: BLE001
            return OpResult(operator=self.operator_id, type="llm",
                            reasoning=f"[LLM算子失败] {type(exc).__name__}: {exc}",
                            confidence=0.0)

        # risk 顾问的 sell/strong_sell → 一票否决位(复现 synthesis.risk_vetoed)
        veto = (self.operator_id == "risk"
                and op.signal in ("sell", "strong_sell"))
        return OpResult(
            operator=self.operator_id, type="llm",
            signal=op.signal, score=float(op.score_adjustment),
            confidence=op.confidence, reasoning=op.reasoning,
            evidence=op.evidence or {}, target_weight=op.target_weight,
            tools_used=getattr(op, "tools_used", []),
            veto=veto,
        )
