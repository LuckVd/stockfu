"""顾问基类 + 共享数据上下文(operators/llm 镜像,逐字复制自 skills/advisors/base.py)。

设计原则:顾问是纯粹的"角色 prompt + LLM"逻辑,不直接查库。
取数由调用方(未来的 analyze 流水线)填充 AdvisorContext,顾问只读。
这样 4 个顾问可独立测试、可并行、不耦合数据层。

数据字段全部对应 services/composite.py 的 compute_stock() 返回 + components。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass
class AdvisorContext:
    """喂给顾问的数据包。所有字段可选 —— 缺失时顾问须如实说"无信号"。"""

    code: str
    name: str = ""
    # 回测截止日(None=实盘/今天);tool 取数上界,防未来函数
    as_of: Optional[date] = None
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


@dataclass
class Opinion:
    """顾问产出的标准化意见(结构借鉴 TradingAgents AgentOpinion,非照搬代码)。"""

    advisor: str            # trend / contrarian / risk / valuation
    signal: str             # strong_buy / buy / hold / sell / strong_sell
    score_adjustment: int   # -20 ~ +20
    confidence: float       # 0.0 ~ 1.0
    reasoning: str
    evidence: dict = field(default_factory=dict)
    tools_used: list[dict] = field(default_factory=list)
    target_weight: float | None = None  # 建议仓位占比 0-1(可选,LLM可输出)


_VALID_SIGNALS = {"strong_buy", "buy", "hold", "sell", "strong_sell"}


def _norm_confidence(v) -> float:
    """confidence → 0-1 float。只做无争议类型转换,**不做**语义词(low/high)等有损猜测——
    模型应按宪法 schema 返回 0-1 小数;非数字/解析失败 → 默认 0.5(不崩,也不替模型瞎猜)。"""
    if v is None:
        return 0.5
    if isinstance(v, bool):  # 注意先于 int 判断
        return 1.0 if v else 0.0
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.5
    return f / 100 if f > 1 else f  # 0-100 → 0-1(常见歧义、无损);0-1 原样


def _norm_signal(v) -> str:
    """signal 归一化到标准枚举;别名/非法值兜底为 hold。"""
    s = str(v or "hold").strip().lower()
    s = {"avoid": "sell", "reduce": "sell", "neutral": "hold",
         "strongbuy": "strong_buy", "strongsell": "strong_sell"}.get(s, s)
    return s if s in _VALID_SIGNALS else "hold"


def _norm_score(v) -> int:
    """score 钳制到 -20~+20(宪法铁律第 3 条)。"""
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        n = 0
    return max(-20, min(20, n))


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
        data.pop("as_of", None)  # 内部透传用(date 不可 json 序列化),不进 LLM prompt
        return (
            f"股票:{ctx.code} {ctx.name}\n"
            f"数据:\n{json.dumps(data, ensure_ascii=False, indent=2)}"
        )

    def parse(self, ctx: AdvisorContext, raw: str) -> Opinion:
        """解析 LLM 输出为 Opinion(业务层归一化 + 容错)。

        raw 已由 analyze._to_text 转为 JSON 串(client.chat_json 用 json_repair 修过语法);
        这里再做语义归一化:confidence 兼容数字/百分比/语义词,signal 非法兜底 hold,
        score 钳制 -20~+20,target_weight 归一化到 0-1。LLM 输出不可控,parse 必须防御。
        """
        import json

        parsed = json.loads(raw)
        tw = parsed.get("target_weight")
        if tw is not None:
            try:
                tw = max(0.0, min(1.0, float(tw)))
            except (TypeError, ValueError):
                tw = None
        return Opinion(
            advisor=self.advisor_id,
            signal=_norm_signal(parsed.get("signal")),
            score_adjustment=_norm_score(parsed.get("score_adjustment", 0)),
            confidence=_norm_confidence(parsed.get("confidence")),
            reasoning=parsed.get("reasoning", ""),
            evidence=parsed.get("evidence") or {},
            target_weight=tw,
        )
