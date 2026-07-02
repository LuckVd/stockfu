"""AI 分析入口:取数 → 4 顾问各出意见 → 规则汇总 → LLM 润色成报告。

完整链路。需要 .env 配好 LLM_*;顾问/汇总逻辑见 skills/ 与 synthesis.py。
"""
from __future__ import annotations

from stockfu.ai.client import chat_json
from stockfu.ai.context import build_context
from stockfu.ai.skills.advisors import ALL_ADVISORS
from stockfu.ai.skills.advisors.base import AdvisorContext, Opinion
from stockfu.ai.synthesis import aggregate, narrate


def run_advisor(advisor_cls, ctx: AdvisorContext) -> Opinion:
    """跑单个顾问:拼 prompt → 调 LLM → 解析 Opinion。"""
    a = advisor_cls()
    parsed = chat_json(a.system_prompt(), a.build_user_message(ctx),
                       max_tokens=400, temperature=0.2)
    return a.parse(ctx, _to_text(parsed))


def _to_text(parsed) -> str:
    """chat_json 已返回 dict;parse 期望 raw 文本,这里回灌成 json 串。"""
    import json
    return json.dumps(parsed, ensure_ascii=False)


def analyze(code: str) -> dict:
    """对单只股票跑完整 4 顾问分析。返回 {context, opinions, aggregate, narrative}。"""
    ctx = build_context(code)

    opinions: list[Opinion] = []
    for advisor_cls in ALL_ADVISORS:
        try:
            opinions.append(run_advisor(advisor_cls, ctx))
        except Exception as exc:  # noqa: BLE001
            # 单个顾问失败不阻断整体(降级:记一个 hold 占位)
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
