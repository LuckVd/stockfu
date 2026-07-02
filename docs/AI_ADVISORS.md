# AI 顾问分析模块（`stockfu/ai/`）

## 一句话

4 个风格正交的常驻投资顾问（趋势/逆向/风险/估值）+ 规则汇总 + LLM 润色，
基于 stockfu 已有的情绪指数/估值分位数据，给单只股票一句话决策解读。

## 为什么是 4 顾问，不是别的

调研了 daily_stock_analysis（15 策略路由）、TradingAgents（多空辩论）、PRISM-INSIGHT（13 agent）、FinRobot 后，选定「4 常驻顾问」路线：

| 调研方案 | 为什么没照搬 |
|---|---|
| daily 15 策略招式路由 | 招式需筹码/缠论/题材数据，stockfu 没有，硬上是幻觉 |
| TradingAgents 多空辩论 | 完整辩论状态机太重；取其「对立视角」思想，简化为 4 顾问 |
| PRISM 13 agent | 多为 Telegram/自动交易等 stockfu 不需的能力 |
| FinRobot 四层架构 | 学术 / autogen 生态，过重 |

核心取舍：**只要「视角层」（常驻角色），不要「招式层」（按行情挑的买卖形态）**。
有用的 daily 招式判断条件融化进顾问（如多头排列 → 趋势顾问 checklist），需要缺失数据的直接弃用。
详见 `stockfu/ai/skills/README.md` 的「daily 15 策略归属表」。

## 架构

```
[取数] build_context(code) ── 读 index_snapshot（快，不调网络）
         → AdvisorContext（纯数据包：fear/greed/heat/pe_pct/...）
              ↓
[顾问] 4 个常驻，各出一份 Opinion（每次都跑，不走路由）
         趋势 / 逆向 ⭐ / 风险 / 估值
              ↓
[汇总] aggregate() 纯规则：总分 + 风险一票否决 → final_signal
              ↓
[润色] narrate() LLM 写一段散户可读解读（不重新打分）
```

设计原则：**确定性 + 表达分离**。数字（打分/信号）由规则定，不交给 LLM（避免幻觉）；LLM 只做自然语言表达。

## 4 顾问

| 顾问 | 立场 | 主用数据 |
|---|---|---|
| 趋势 `trend` | 顺势，能否跟 | ma_alignment / heat / today_chg |
| 逆向 `contrarian` ⭐ | 情绪极端唱反调 | 三层 fear/greed（stockfu 独有武器） |
| 风险 `risk` | 永远挑刺，一票否决 | volatility / 估值过热 / 三层过热 |
| 估值 `valuation` | 贵不贵 | PE/PB 分位 / 股息率 |

**逆向顾问是差异化点**：别人的「唱空」靠新闻/Reddit 找利空，我们的唱空靠自己的情绪分位（greed≥75 = 过热该跌），数字更硬。

4 顾问共用一份「口径宪法」（`constitution.py`）：fear/greed/heat 定义、75/55/45/25 分档、PE 分位口径，保证它们说同一套话。

## 文件清单

```
stockfu/ai/
├── client.py          # OpenAI 兼容 LLM 调用（httpx + json_repair + 重试 + 自动补 /v1）
├── context.py         # 取数：读 index_snapshot 填 AdvisorContext
├── analyze.py         # 入口：取数 → 4 顾问 → 汇总 → 润色
├── synthesis.py       # 规则汇总 + LLM 润色
└── skills/
    ├── constitution.py    # 口径宪法（4 顾问共用）
    ├── advisors/
    │   ├── base.py            # AdvisorContext + Opinion + BaseAdvisor
    │   ├── trend.py / contrarian.py / risk.py / valuation.py
    │   └── __init__.py        # ALL_ADVISORS 清单
    └── README.md          # 详细说明 + daily 15 策略归属表
```

## 配置

`.env`（已 gitignore，密钥不进库）：

```ini
LLM_BASE_URL=https://opencode.ai/zen/go      # client 自动补 /v1/chat/completions
LLM_API_KEY=sk-...                            # 你的 key
LLM_MODEL=glm-5.2                             # 或其他该 key 授权的模型
```

运行：

```bash
python3 -c "from stockfu.ai.analyze import analyze; import json; print(json.dumps(analyze('600519'), ensure_ascii=False, indent=2))"
```

## 数据口径

`AdvisorContext` 字段全部来自 services，非臆造：

- fear/greed/heat ← `composite.compute_stock()`（0-100 历史分位）
- pe_pct/pb_pct ← `compute_stock()` 的 `components`
- 分位计算 ← `factors.percentile()`（样本 <10 返回 None）

顾问据此判断：缺失数据如实说「无信号」，严禁编造。

## 当前状态（2026-07-02）

已完成并验证：

- ✅ 4 顾问 + 宪法 + 基类（可加载，prompt 正确拼装）
- ✅ `client.py`（路径/认证/重试/json_repair，自动补 `/v1`）
- ✅ `context.py`（读 600519 真实数据通过：fear=68.81 / greed=28.2 / pe_pct=0.18 …）
- ✅ `synthesis.py`（规则汇总 + 风险一票否决，dry-run 验证）
- ✅ `analyze.py`（完整链路入口）

待办：

- ⬜ **LLM key**：测试 key 对 opencode.ai 的 chat 端点报 401（误导性 AuthError，实为该 key 对所测 model 无权限——`opencode-go/` 前缀能过 auth 报 ModelError 可证 key 本身有效）；待配授权范围内的 model/key 后一键跑通
- ⬜ **pe_pct/pb_pct 口径**：实测值 0.18 / 0.76 疑似 0-1 小数而非 0-100，会让估值顾问失真，需核 baostock `get_pe_pb_percentile` 返回值并 ×100 对齐
- ⬜ **reflection**：决策反思落库 + 下次注入（借 TradingAgents「2-4 句精简」哲学）
- ⬜ **tools/**：技术分析工具（均线排列算 `ma_alignment`、MACD/RSI），供趋势顾问调用
- ⬜ **API/前端**：`/ai_report/{code}` 端点 + 持仓表 AI 解读列

## 参考来源（只借思想，不抄代码）

- **TradingAgents**（TauricResearch）：4 分析师分工、对立视角、reflection 精简哲学
- **PRISM-INSIGHT**：regime 检测、自我改进 trading journal 思想
- **FinRobot**：Financial CoT、按任务选模型
- **daily_stock_analysis**（本地）：口径宪法机制、skill 抽象

参考资料（外部源码拷贝）存放于仓库根 `references/`，**未纳入 git**（license 合规：PRISM 为 AGPL，FinRobot 为 Apache，TradingAgents 见各自 LICENSE）。仅作设计参考。
