"""综合决策:把 4 顾问 Opinion 合成最终建议。

设计(确定性 + 表达分离):
- aggregate():纯规则汇总,不调 LLM。总分 + 风险一票否决 → final_signal。
  数字必须确定性,不交给 LLM 算(避免幻觉/不可解释)。
- narrate():LLM 只把"汇总结果 + 4 顾问理由"写成一段散户可读的话,
  明令不得推翻 final_signal、不得给具体价位。

参考 TradingAgents research_manager 的"合成"思想,但不抄其辩论结构。
"""
from __future__ import annotations

from stockfu.ai.client import chat
from stockfu.ai.skills.advisors.base import Opinion

# 总分 → 信号阈值(score_adjustment 各顾问 -20~+20,4 顾问合计理论 -80~+80)
_THRESHOLDS = [
    (15, "strong_buy"),
    (5, "buy"),
    (-5, "hold"),
    (-15, "sell"),
]


def aggregate(opinions: list[Opinion]) -> dict:
    """规则汇总。风险顾问 sell/strong_sell 触发一票否决(压过所有看多)。"""
    total = sum(o.score_adjustment for o in opinions)
    risk = next((o for o in opinions if o.advisor == "risk"), None)
    vetoed = bool(risk and risk.signal in ("sell", "strong_sell"))

    if vetoed:
        final = "strong_sell" if risk.signal == "strong_sell" else "sell"
    else:
        final = "hold"
        for threshold, sig in _THRESHOLDS:
            if total >= threshold:
                final = sig
                break
        else:
            final = "strong_sell"

    return {
        "final_signal": final,
        "total_score": total,
        "risk_vetoed": vetoed,
        "opinions": [
            {
                "advisor": o.advisor,
                "signal": o.signal,
                "score": o.score_adjustment,
                "confidence": o.confidence,
                "reasoning": o.reasoning,
            }
            for o in opinions
        ],
    }


def narrate(agg: dict, *, max_tokens: int = 500) -> str:
    """LLM 把汇总 + 4 顾问理由写成一段散户可读的解读。不重新打分。"""
    sys = (
        "你是 stockfu 综合分析师。下面是 4 个顾问(趋势/逆向/风险/估值)对一只股票的意见与规则汇总。"
        "请写一段 150 字以内的综合解读,要求:\n"
        "1) 以规则汇总的 final_signal 为准,严禁推翻;\n"
        "2) 点出最主要的一条看多理由和一条看空理由;\n"
        "3) 若 risk_vetoed=true,必须醒目提示【风险顾问一票否决】;\n"
        "4) 严禁给出具体买卖价位;只输出这段话,不要 JSON、不要标题。"
    )
    ops = "\n".join(
        f"- {o['advisor']}({o['signal']},调整{int(o['score']):+d}):{o['reasoning']}"
        for o in agg["opinions"]
    )
    user = (
        f"规则汇总:final_signal={agg['final_signal']}, total_score={agg['total_score']}, "
        f"risk_vetoed={agg['risk_vetoed']}\n4 顾问意见:\n{ops}"
    )
    return chat(
        [{"role": "system", "content": sys}, {"role": "user", "content": user}],
        max_tokens=max_tokens,
        temperature=0.4,
    )
