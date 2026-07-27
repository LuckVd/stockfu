# AI 顾问分析模块（`stockfu/ai/`）

## 一句话

4 个风格正交的常驻投资顾问（趋势/逆向/风险/估值）+ 规则汇总 + LLM 润色，
基于 stockfu 已有的情绪指数/估值分位数据，给单只股票一句话决策解读。

> 实盘分析走 `ai.analyze.analyze`（4顾问在 `ai/skills/advisors/`，独立链路）。回测不使用LLM顾问；回测边界与正式准入要求统一见 `docs/BACKTEST.md`。

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
[取数] build_context(code) ── 读 index_snapshot + quote_snapshot + ...（快，不调网络）
         ↓
[工具] run_with_tools() ── 工具循环
         │  每个顾问可见的工具列表不同（USED_BY 权限控制）
         │  7 个分析工具：ma_alignment / macd / rsi / bollinger / volume_price / support_resistance / volatility
         ↓
[顾问] 4 个常驻，各出一份 Opinion（每次都跑，不走路由）
         趋势 / 逆向 ⭐ / 风险 / 估值
              ↓
[汇总] aggregate() 纯规则：总分 + 风险一票否决 → final_signal
              ↓
[润色] narrate() LLM 写一段散户可读解读（不重新打分）
```

设计原则：**确定性 + 表达分离**。数字（打分/信号）由规则定，不交给 LLM（避免幻觉）；LLM 只做自然语言表达。

## 评分与投票机制

### 各专家权限
- 每位专家有 **±20 分** 的调整权限（`score_adjustment`）
- 4 位专家 **等权**，总分 = 简单求和

### 信号阈值

| 总分范围 | 最终信号 |
|----------|----------|
| ≥ +15 | **strong_buy** |
| ≥ +5 | **buy** |
| ≥ -5 | **hold** |
| ≥ -15 | **sell** |
| < -15 | **strong_sell** |

实现见 `synthesis.py:aggregate()`。

### 一票否决（特殊规则）

**风险顾问**的 `sell` / `strong_sell` 可以覆盖总分结果，强制修改最终信号：

```python
if risk.signal in ("sell", "strong_sell"):
    final = risk.signal  # 无视总分，强制改为风险建议
```

这条规则设计目的是：其他顾问可能集体看多（如估值发现极度低估），但风险找到了硬风险（波动异常/系统性过热），应能被"拉响警报"。

### 评分示例

平安 601318 某次分析：

| 顾问 | 得分 | 理由 |
|------|------|------|
| 趋势 | -12 | 空头排列+跌破支撑+热度极端 |
| 逆向 | +8 | 恐慌74.3逼近极端+PE 0.59%史低 |
| 风险 | -10 | ⚠️ 波动率90.97分位极端 → 一票否决 |
| 估值 | +10 | PE 0.59%+股息5.5%=极度便宜 |
| **总分** | **-4** | 按阈值应为 hold，但风险否决 → **final=sell** |

## 工具系统（`skills/tools/`）

### 设计
- 工具是纯本地分析函数，不接受 `code` 参数（由框架注入），只接受分析维度参数
- 每个工具注册时声明 `USED_BY`：哪些顾问可见
- 工具对顾问不可见 = 顾问 prompt 里不会出现该工具的说明

### 调用流程

```
run_with_tools(advisor_cls, ctx):
  ① 取该顾问的可见工具列表     → get_tools_for("trend")
  ② 拼接 system prompt + 工具描述
  ③ 调 LLM（带 tools 参数）
  ④ LLM 返回 tool_calls 或 stop
  ⑤ 如果是 tool_calls → 执行工具 → 结果回传 → 回到③
  ⑥ 如果是 stop → parse Opinion → 返回
```

### 已注册工具

| 工具 | 输入参数 | 输出 | 谁用 |
|------|----------|------|------|
| `ma_alignment` | short_ma, mid_ma, long_ma | 多头/空头/中性排列 | 趋势, 风险 |
| `macd` | fast, slow, signal | 金叉/死叉/零轴/柱线放缩 | 趋势, 逆向, 风险 |
| `rsi` | period | 超买/超卖/中性 | 逆向, 风险 |
| `bollinger` | period, std_dev | 上/中/下轨位置, 带宽 | 趋势, 逆向, 风险 |
| `volume_price` | lookback, vol_threshold | 量价配合/背离/放量异常 | 趋势 |
| `support_resistance` | lookback, buckets | 支撑/阻力价位+触碰次数 | 趋势, 逆向 |
| `volatility` | period | ATR+波动率历史分位 | 风险 |

### 调用记录

每个顾问每次分析完整记录调用了什么工具、传了什么参数、返回了什么结果，存于 `Opinion.tools_used` 数组，随输出返回。

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
│                     # + chat_completion() 支持 function calling（tool_calls 多轮循环）
├── context.py         # 取数：读 index_snapshot + quote_snapshot + dividend_event 填 AdvisorContext
│                     # + CODE_SECTOR_FALLBACK 板块映射表
├── analyze.py         # 入口：取数 → run_with_tools(4 顾问, 含工具循环) → 汇总 → 润色
├── synthesis.py       # 规则汇总（总分+风险一票否决）+ LLM 润色
└── skills/
    ├── constitution.py    # 口径宪法（4 顾问共用：字段类型、分档阈值、输出 schema 示例）
    ├── advisors/
    │   ├── base.py            # AdvisorContext + Opinion（含 tools_used）+ BaseAdvisor + parse 归一化
    │   ├── trend.py / contrarian.py / risk.py / valuation.py
    │   └── __init__.py        # ALL_ADVISORS 清单
    ├── tools/                 # 7 个分析工具（注册表 + function calling schema）
    │   ├── __init__.py        # 注册表：discover_and_register / get_tools_for / execute_tool / TOOL_CALL_LOG
    │   ├── ma_alignment.py    # MA5/10/20 排列（趋势/风险）
    │   ├── macd.py            # 金叉/死叉/柱线/零轴（趋势/逆向/风险）
    │   ├── rsi.py             # 超买/超卖/中性（逆向/风险）
    │   ├── bollinger.py       # 轨道位置/带宽（趋势/逆向/风险）
    │   ├── volume_price.py    # 量价配合/背离（趋势）
    │   ├── support_resistance.py # 支撑/阻力价位（趋势/逆向）
    │   └── volatility.py      # ATR+波动率分位（风险）
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

## 当前状态（2026-07-03）

### 已完成并验证

- ✅ **4 顾问完整链路跑通**（deepseek-v4-flash via opencode.ai，单股 ~100s）
- ✅ **LLM 直连**：`client.py` use_proxy 默认 False，走 7890 代理 SSL 失败已解决
- ✅ **reasoning 模型适配**：max_tokens 调至 10 万（reasoning_tokens 计入预算）
- ✅ **parse 输出归一化**：confidence/signal/score 防御性解析，`_norm_confidence` 不做有损语义词映射
- ✅ **口径宪法**：铁律 4 写死字段类型 + 示例，4 顾问统一引用
- ✅ **context 字段补全**：sector fallback、today_chg、ma_alignment、profit_pct、dividend_yield
- ✅ **_call_timeout 调大**：8s→20s，baostock PE/PB 不再超时丢失
- ✅ **7 个分析工具**（function calling 循环，各顾问按 USED_BY 权限可见）
- ✅ **工具调用记录**：每次分析记录 `tools_used[{tool, args, result}]`
- ✅ **PE/PB 口径确认**：baostock 返回 0-100 分位（实测 0.18 → 18% 偏便宜）
- ✅ **dividend_yield 口径确认**：per_share_cash 已是每股值（数据源已 ÷10），勿再除

### 待办

- ⬜ **reflection**：决策反思落库 + 下次注入（借 TradingAgents「2-4 句精简」哲学）
- ⬜ **API/前端**：`/ai_report/{code}` 端点 + 持仓表 AI 解读列
- ⬜ **美股数据补全**：AAPL 等 quote_snapshot 为空，需修 yfinance 抓取
- ⬜ **板块轮动工具**(future)：等 sector_flow_snapshot 累积足够历史后加

## 参考来源（只借思想，不抄代码）

- **TradingAgents**（TauricResearch）：4 分析师分工、对立视角、reflection 精简哲学
- **PRISM-INSIGHT**：regime 检测、自我改进 trading journal 思想
- **FinRobot**：Financial CoT、按任务选模型
- **daily_stock_analysis**（本地）：口径宪法机制、skill 抽象

参考资料（外部源码拷贝）存放于仓库根 `references/`，**未纳入 git**（license 合规：PRISM 为 AGPL，FinRobot 为 Apache，TradingAgents 见各自 LICENSE）。仅作设计参考。
