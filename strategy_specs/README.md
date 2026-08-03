# strategy_specs

各策略的**客观机制规格**文档。每个文件描述一个策略可被还原的全部参数与流程,不含回测绩效。

## 命名约定

- 文件名 = `<strategy_id>.md`,与 `stockfu/ai/strategies/<strategy_id>.yaml` 同名一一对应。
- 改 yaml 后,同步更新对应 spec(尤其 params / risk 段)。

## 文档结构(模板)

见 `dividend_cross_section.md`,共 14 节:

1. 标识
2. 配置参数总表(算子 / 聚合 / 仓位 / 去抖 / 风控 / 执行 / 费用 / 宇宙)
3. 选股宇宙
4. 因子算子(输入 / 计算 / score 映射 / 取值范围)
5. 聚合
6. 仓位映射
7. 止损
8. rebalancer
9. 组合层风控
10. 去抖
11. 执行
12. 费用
13. 估值
14. 端到端示例

通用项(所有策略一致):费用常量、执行规则、宇宙规则、估值口径 —— 见 `dividend_cross_section.md` §2.6–2.8、§11–13;策略差异主要在 §2.1–2.5(算子 / 聚合 / 仓位 / 去抖 / 风控)与 §4–8。

## 策略索引

### 横截面(cap_and_rank rebalancer)

| strategy_id | 名称 | 因子 | spec |
|---|---|---|---|
| dividend_cross_section | 红利横截面 | dividend_yield + low_volatility + value | ✅ |
| momentum_breakout_cross_section | 动量突破横截面 | monthly_bollinger + momentum + trend_strength | ✅ |
| cn_momentum_cross_section | 个股动量横截面 | momentum + trend_linearity + trend_strength | ✅ |
| etf_momentum_cross_section | ETF动量横截面 | momentum + trend_linearity + trend_strength | ✅ |
| cross_section_factor | 横截面多因子 | reversal + low_volatility + value | — |
| reversal_cross_section | 反转横截面 | reversal + mean_reversion + value | — |
| bollinger_reversion_cross_section | 布林回归横截面 | daily_bollinger + weekly_bollinger + mean_reversion + trend_strength | — |

#### 2026-08 网络调研新增族(10 个,规格见 [`NEW_STRATEGIES_2026.md`](NEW_STRATEGIES_2026.md))

| strategy_id | 名称 | 主因子 |
|---|---|---|
| fifty_two_week_high_cross_section | 52周新高横截面 | fifty_two_week_high + momentum + trend_strength |
| small_cap_low_turnover | 小盘低换手 | size + low_turnover + reversal |
| low_turnover_reversal | 低换手反转 | low_turnover + reversal + value |
| illiquidity_value | Amihud流动性价值 | illiquidity + value + reversal |
| anti_lottery_defensive | 反彩票防御 | lottery_max + low_volatility + value |
| low_beta_dividend | 低贝塔红利 | low_beta + dividend_yield + value |
| ts_momentum_trend | 时序动量趋势 | ts_momentum + momentum_acceleration + trend_strength |
| graham_defensive_value | 格雷厄姆防御价值 | graham_value + dividend_yield + low_volatility |
| donchian_breakout_cross_section | 唐奇安突破横截面 | donchian_breakout + ts_momentum + trend_strength |
| smart_beta_multi_factor | 智能贝塔多因子 | 52w_high + low_vol + value + low_turnover + graham_value |

### 轮动 / 锁仓(top_n_picker rebalancer)

| strategy_id | 名称 | 因子 | spec |
|---|---|---|---|
| momentum_breakout | 月线动量突破 | monthly_bollinger + momentum + trend_strength | ✅ |
| cn_momentum_rotation | 降换手动量轮动 | momentum + trend_linearity + trend_strength | — |
| etf_momentum_rotation | ETF动量轮动 | momentum + trend_linearity + trend_strength | ✅ |
| dividend_low_vol | 红利低波 | dividend_yield + low_volatility + value | — |
| reversal_strategy | 反转均值回归 | reversal + mean_reversion + value | — |
| dual_bollinger | 双布林带(周+月) | weekly_bollinger + monthly_bollinger + momentum | ✅ |
| bollinger_reversion | 布林均值回归 | daily_bollinger + weekly_bollinger + mean_reversion + trend_strength | — |
| macd_cross | MACD金叉死叉 | macd_cross | — |
| pure_factor | 纯因子动量反转 | momentum + mean_reversion + trend_strength + value | — |

## 状态图例

- ✅ 已完成规格文档
- — 待补
