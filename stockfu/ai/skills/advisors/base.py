"""顾问基类 + 共享数据上下文。

设计原则:顾问是纯粹的"角色 prompt + LLM"逻辑,不直接查库。
取数由调用方(未来的 analyze 流水线)填充 AdvisorContext,顾问只读。
这样 4 个顾问可独立测试、可并行、不耦合数据层。

数据字段全部对应 services/composite.py 的 compute_stock() 返回 + components。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AdvisorContext:
    """喂给顾问的数据包。所有字段可选 —— 缺失时顾问须如实说"无信号"。"""

    code: str
    name: str = ""
    # 个股层情绪(0-100,来自 compute_stock)
    fear: Optional[float] = None
    greed: Optional[float] = None
    heat: Optional[float] = None
    # 三层共振:市场层 / 板块层(同口径,来自 compute_market/compute_sector)
    market_fear: Optional[float] = None
    market_greed: Optional[float] = None
    sector_fear: Optional[float] = None
    sector_greed: Optional[float] = None
    # 估值分位(近10年,来自 compute_stock components: pe_pct/pb_pct)
    pe_pct: Optional[float] = None
    pb_pct: Optional[float] = None
    dividend_yield: Optional[float] = None
    # 行情(来自 compute_stock today_chg + 技术工具填充的均线)
    today_chg: Optional[float] = None
    ma_alignment: Optional[str] = None  # bullish/neutral/bearish(趋势工具填充)
    volatility_pct: Optional[float] = None
    # 持仓视角(可选,来自 portfolio.PositionView)
    has_position: bool = False
    profit_pct: Optional[float] = None


@dataclass
class Opinion:
    """顾问产出的标准化意见(结构借鉴 TradingAgents AgentOpinion,非照搬代码)。"""

    advisor: str            # trend / contrarian / risk / valuation
    signal: str             # strong_buy / buy / hold / sell / strong_sell
    score_adjustment: int   # -20 ~ +20
    confidence: float       # 0.0 ~ 1.0
    reasoning: str
    evidence: dict = field(default_factory=dict)


class BaseAdvisor:
    """4 顾问基类。子类只需实现 system_prompt()。"""

    advisor_id: str = "base"
    display_name: str = "基础顾问"

    def system_prompt(self) -> str:
        raise NotImplementedError

    def build_user_message(self, ctx: AdvisorContext) -> str:
        """把 ctx 序列化成给 LLM 的数据段。子类一般不用改。"""
        import json

        data = {k: v for k, v in ctx.__dict__.items() if v not in (None, "", False)}
        data["code"] = ctx.code  # code 必带
        return (
            f"股票:{ctx.code} {ctx.name}\n"
            f"数据:\n{json.dumps(data, ensure_ascii=False, indent=2)}"
        )

    def parse(self, ctx: AdvisorContext, raw: str) -> Opinion:
        """解析 LLM 输出为 Opinion。

        TODO: 接入 json_repair 容错(见 references/tradingagents README 的鲁棒性设计)。
        当前用标准 json.loads,等 stockfu/ai/client.py 建好后替换。
        """
        import json

        parsed = json.loads(raw)  # noqa: 后续换 json_repair.repair_json(raw)
        return Opinion(
            advisor=self.advisor_id,
            signal=parsed.get("signal", "hold"),
            score_adjustment=int(parsed.get("score_adjustment", 0)),
            confidence=float(parsed.get("confidence", 0.5)),
            reasoning=parsed.get("reasoning", ""),
            evidence=parsed.get("evidence", {}),
        )
