# stockfu AI skill 体系

主线架构:**4 个风格正交的常驻顾问 + 多个技术分析工具**,不抄 TradingAgents 的辩论框架,也不抄 daily 的 15 策略路由。

```
[取数层] compute_stock/market/sector → AdvisorContext(纯数据包)
    ↓
[顾问层] 趋势 / 逆向 / 风险 / 估值  ── 各出一份 Opinion(常驻,每次都跑)
    ↓
[综合层] synthesis 把 4 份 Opinion 合成最终建议(TODO)
    ↓
[记忆层] reflection 落库 + 下次注入(TODO)
```

## 4 个顾问

| 顾问 | 立场 | 主用数据 | 参考 |
|---|---|---|---|
| 趋势 `trend.py` | 顺势,能不能跟 | ma_alignment/heat/today_chg | TradingAgents technical_analyst + daily 趋势理念 |
| 逆向 `contrarian.py` ⭐ | 情绪极端唱反调 | 三层 fear/greed | TradingAgents bear_researcher(只借"强制找反面"技巧) |
| 风险 `risk.py` | 永远挑刺,一票否决 | volatility/估值过热/三层过热 | TradingAgents risk_mgmt + daily 风险排查 |
| 估值 `valuation.py` | 贵不贵 | PE/PB 分位/股息率 | TradingAgents fundamentals_analyst + daily 估值 |

逆向顾问是 stockfu 的差异化点 —— TradingAgents 的 bear 靠新闻/Reddit 找利空,我们的 bear 靠**自己的情绪分位**(greed≥75=过热该跌),数字更硬。

## daily 15 策略的归属(详见对话结论)

15 个策略**不作为独立 skill**,而是:有用的"判断条件"融化进顾问,需要 stockfu 没有的数据(筹码/缠论/题材/新闻)的直接弃用。

| daily 策略 | 去向 |
|---|---|
| bull_trend / ma_golden_cross / volume_breakout | → 趋势顾问 checklist |
| shrink_pullback(缩量回踩买点) | → 趋势顾问买点判断 |
| bottom_volume(地量见底) | → 逆向顾问佐证 |
| growth_quality | → 估值顾问 |
| chan_theory / wave_theory / dragon_head / hot_theme / emotion_cycle / event_driven / expectation_repricing / one_yang_three_yin | ❌ 弃用(数据不支持) |

## 数据接口(真实,对应 services)

`AdvisorContext` 字段全部来自 `composite.compute_stock()` 返回 + 其 `components`:

| 顾问字段 | 来源 |
|---|---|
| fear/greed/heat | `compute_stock(code)["fear"/"greed"/"heat"]` |
| market_fear/greed | `compute_market()["fear"/"greed"]` |
| sector_fear/greed | `compute_sector(etf, name)["fear"/"greed"]` |
| pe_pct/pb_pct | `compute_stock(code)["components"]["pe_pct"/"pb_pct"]` |
| volatility_pct | `components["volatility_pct"]` |
| today_chg | `compute_stock(code)["today_chg"]` |
| 分位计算 | `services.factors.percentile(series, value)`(样本<10 返回 None) |

## 还没做(TODO)

- [ ] `stockfu/ai/client.py` —— OpenAI 兼容调用 + json_repair 容错 + 超时重试
- [ ] `stockfu/ai/skills/tools/` —— 技术分析工具(均线排列算 `ma_alignment`、MACD/RSI),供顾问调用
- [ ] `stockfu/ai/synthesis.py` —— 综合 4 顾问 Opinion 出最终建议(参考 TradingAgents research_manager 的合成思想,但不抄辩论)
- [ ] `stockfu/ai/reflection.py` —— 决策反思落库 + 下次注入(借 TradingAgents "2-4 句精简"哲学)
- [ ] 取数适配器 —— 把 `compute_stock` 等结果填进 `AdvisorContext` 的函数

## 参考与合规

- 参考资料(只读不抄)在仓库根 `references/`:TradingAgents / PRISM-INSIGHT / FinRobot
- `references/` 建议加入 `.gitignore`(外部代码拷贝不进自己 git 历史)
- 顾问 prompt 全部用 stockfu 口径中文重写,**不是**任何参考项目的英文原文
