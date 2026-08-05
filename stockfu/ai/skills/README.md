# StockFu AI skill 体系

StockFu 的 AI 链路由 4 个风格正交的常驻顾问、7 个本地技术分析工具和一个确定性汇总器组成。顾问只使用 StockFu 已有的数据与工具；LLM 负责解释，规则负责打分和最终信号。

## 链路

```
build_context(code)
    ↓
run_with_tools() → 趋势 / 逆向 / 风险 / 估值顾问
    ↓
aggregate() → 总分 + 风险一票否决 + final_signal
    ↓
narrate() → 可选的 LLM 中文润色
```

实盘入口是 `stockfu.ai.analyze.analyze`。回测不调用这条 LLM 链路，回测规则统一见 `docs/BACKTEST.md`。

## 4 个顾问

| 顾问 | 立场 | 主用数据 |
|---|---|---|
| 趋势 `trend.py` | 顺势，判断能否跟随 | 均线排列、热度、涨跌、量价 |
| 逆向 `contrarian.py` | 情绪极端时寻找反向证据 | 个股/市场/板块恐贪分位、MACD、RSI |
| 风险 `risk.py` | 主动挑出硬风险 | 波动率、布林带、估值过热、三层热度 |
| 估值 `valuation.py` | 判断价格是否昂贵 | PE/PB 分位、股息率 |

4 位顾问等权输出 `Opinion`，每位可以调整 -20 到 +20 分。汇总阈值为：`strong_buy >= 15`、`buy >= 5`、`hold >= -5`、`sell >= -15`，其余为 `strong_sell`。风险顾问给出 `sell` 或 `strong_sell` 时，一票否决总分信号。

## 7 个本地工具

| 工具 | 作用 | 可见顾问 |
|---|---|---|
| `ma_alignment` | 判断短中长均线多空排列 | 趋势、风险 |
| `macd` | 判断金叉/死叉、零轴与柱线 | 趋势、逆向、风险 |
| `rsi` | 判断超买、超卖或中性 | 逆向、风险 |
| `bollinger` | 判断轨道位置与带宽 | 趋势、逆向、风险 |
| `volume_price` | 判断量价配合、背离与异常放量 | 趋势 |
| `support_resistance` | 计算支撑/阻力价位与触碰次数 | 趋势、逆向 |
| `volatility` | 计算 ATR 与历史波动率分位 | 风险 |

工具是纯本地分析函数，不直接接收股票代码；框架注入上下文并按 `USED_BY` 控制可见范围。完整调用记录保存在 `Opinion.tools_used`。

## 目录

```
stockfu/ai/
├── client.py                 # OpenAI 兼容调用、重试与 function calling
├── context.py                # 从本地快照构建 AdvisorContext
├── analyze.py                # 取数、顾问工具循环、汇总、润色
├── synthesis.py              # 确定性汇总与 LLM 叙述
└── skills/
    ├── constitution.py       # 统一字段与分档口径
    ├── advisors/             # 4 个顾问及 Opinion 解析
    └── tools/                # 工具注册表与 7 个分析工具
```

配置通过 `.env` 提供 `LLM_BASE_URL`、`LLM_API_KEY` 和 `LLM_MODEL`。缺失数据必须输出“无信号”，不能由前端或 LLM 臆造。

## daily 15 策略的取舍

可由现有数据表达的条件融入顾问：趋势、均线金叉、量价突破、缩量回踩进入趋势顾问；地量见底进入逆向顾问；成长质量进入估值顾问。缠论、波浪、龙头、题材、事件驱动等需要 StockFu 当前没有的数据，因此不作为独立 skill。

## 尚未实现

`reflection`（决策反思落库并注入下一次分析）仍是后续能力；它不影响当前 4 顾问、工具、规则汇总和 API/前端报告链路。
