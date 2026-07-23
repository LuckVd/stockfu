# etf_momentum_cross_section 策略规格

对应配置:`stockfu/ai/strategies/etf_momentum_cross_section.yaml`。
客观规格,不含绩效。

## 1. 标识

| 项 | 值 |
|---|---|
| strategy_id | etf_momentum_cross_section |
| rebalancer | cap_and_rank |
| universe | etf(EtfQuoteDaily) |
| 估值/成交价格口径 | 前复权 `*_qfq` |
| 基准 | sh000001 |
| 初始资金 | 1,000,000 |

## 2. 配置参数总表

### 2.1 算子(operators)

| id | type | weight | params |
|---|---|---|---|
| momentum | math | 1.0 | {window: 20} |
| trend_linearity | math | 0.6 | {window: 20} |
| trend_strength | math | 0.4 | — |

### 2.2 聚合(aggregate)

- method: weighted_sum
- thresholds: {strong_buy: 12, buy: 4, hold: -4, sell: -12}

### 2.3 仓位(position)

| 参数 | 值 |
|---|---|
| mode | continuous |
| max_w | 0.05 |
| dead | 3.0 |
| score_full | 8 |

### 2.4 去抖(debounce)

| 参数 | 值 |
|---|---|
| buy_cool_down_days | 1 |
| sell_cooldown_days | 1 |
| max_target_step | 1.0 |
| risk_confirm_days | 1 |
| min_trade_weight | 0.01 |
| conf_gate | 0.0 |

### 2.5 风控与资金

| 参数 | 值 | 来源 |
|---|---|---|
| max_gross | 0.95 | rebalancer_params |
| stop_loss | 0.08 | 默认 DEFAULT_STOP_LOSS |
| portfolio_brake | 0.10 | 默认 DEFAULT_PORTFOLIO_BRAKE |
| edge_threshold | 0.005 | PositionManager 默认 |

### 2.6 执行(execution_rules)

| 参数 | 值 |
|---|---|
| execution | T+1_open_sell_first |
| strict | False |
| limit_rule | True |
| slip_bps | 10 |
| on_unfillable | defer |
| limit_tol | 0.15 |

### 2.7 费用常量(engine.py)

| 常量 | 值 |
|---|---|
| COMMISSION_RATE | 0.0003(双边) |
| MIN_COMMISSION | 5.0 |
| STAMP_DUTY_RATE | 0.0005(卖出,as_of ≥ 2023-08-28) |
| STAMP_DUTY_RATE_OLD | 0.001(卖出,as_of < 2023-08-28) |
| TRANSFER_FEE_RATE | 0.00001(双边) |

### 2.8 宇宙规则

universe_rules = None(strict=False)。ETF 无 ST/上市天数/成交额过滤概念。

## 3. 选股宇宙

基础池 = `EtfQuoteDaily.asset_code` 去重(`_resolve_codes` 中 `universe == "etf"` 分支)。每个交易日 T:

```
U(T) = { code | code 当日有 close }
```

(strict=False:engine 用 `set(close_prices.keys())`,不做 ST/停牌/上市天数/成交额过滤。)

板块判定仍适用:ETF 一般 main(10% 涨跌停),少数 ETF 20%。

## 4. 因子算子

### 4.1 momentum

输入 `quote_series(code, "close", 50, as_of)`(qfq):

```
ret = (closes[−1]/closes[−20] − 1) × 100
score = ret × 2                      # 不 clamp
signal = buy if ret>3 else sell if ret<−3 else hold
confidence = 0.7
```

### 4.2 trend_linearity

输入 `quote_series(code, "close", 35, as_of)`(qfq),对 closes[-20:] 线性回归:

```
(r2, slope) = linreg_r2(closes[−20:])
score = r2 × sign(slope) × 20        # ∈ [−20, 20]
signal = buy  if r2>0.6 and slope>0
       sell if r2>0.6 and slope<0
       hold otherwise
confidence = r2
```

### 4.3 trend_strength

输入 `ma_alignment(code, lookback=250, as_of)`(MA5/10/20):

```
bullish(MA5>MA10>MA20) → score = +20, buy,  confidence=0.7
bearish(MA5<MA10<MA20) → score = −20, sell, confidence=0.7
otherwise              → score = 0,   hold, confidence=0.5
```

## 5. 聚合(weighted_sum)

```
total_score = round(momentum×1.0 + trend_linearity×0.6 + trend_strength×0.4, 2)
confidence  = mean(三算子 confidence)
risk_vetoed = False
final_signal = score_to_signal(total_score, thresholds):
    ≥ 12 → strong_buy;  ≥ 4 → buy;  ≥ −4 → hold;  ≥ −12 → sell;  < −12 → strong_sell
```

final_signal 不参与仓位决策。

## 6. 仓位映射(compute_target_weight)

```
risk_vetoed → 0.0                                    # 恒 False
_total_to_weight(total_score, max_w=0.05, dead=3.0, score_full=8):
    total ≤ −3     → 0.0
    −3 < total < 3 → None
    total ≥ 3      → round(0.05 × min(total/8, 1.0), 4)
```

| total_score | 目标权重 |
|---|---|
| ≤ −3 | 0 |
| (−3, 3) | None |
| 3 | 0.0188 |
| 4 | 0.0250 |
| 6 | 0.0375 |
| ≥ 8 | 0.0500 |

## 7. 个股止损(stop_loss = 0.08)

```
若 current_weight > 0 且 target ∉ {0, None}:
    若 close(as_of)/avg_cost − 1 ≤ −0.08 → target = 0, signal = stop_loss
```

## 8. rebalancer(cap_and_rank)

输入 desired、current、meta(raw = total_score)、max_gross = 0.95。

```
pct[c] = raw 升序排名平均秩 → [0,1]
desired = None     → final = None(维持)
desired ≤ current  → final = desired(放行)
desired > current  → 入池 priority = pct[c] × confidence[c]
running_gross = Σ final(已定项)
按 (−priority, code) 遍历增仓池:
    若 running_gross + (desired−current) ≤ max_gross → final=desired, 累计
    否则 → final = current(维持)
```

## 9. 组合层风控

- 组合刹车:peak = max(peak, equity);`equity/peak − 1 ≤ −0.10` → final[c] × 0.5。
- 总仓安全阀:Σ正值 > 0.95 → 正值等比缩放至 Σ=0.95。
- 宇宙外持仓:final[c] > current[c] → final[c] = current[c]。

## 10. 去抖(PositionManager.should_act)

按 code 排序逐票:`max_target_step=1.0`;`min_w=max(0.005,0.01)` 时 `|current−target|<min_w` 不下单;`edge=0.005` 时 `|target−executed|<edge` 不下单;增仓 `buy_cool_down_days=1`;部分减仓 `sell_cooldown_days=1`(清仓不限)。通过 → pending_target[code]=target。

## 11. 执行(T+1 开盘)

成交价 = open(T+1);无 open → close(T+1);均无 → 0(顺延)。check_fill:price≤0→no_price;trade_status=0→suspended;涨停(open_pct ≥ limit_pct−0.15)→拒买;跌停→拒卖;limit_pct = main 10(ETF)/ star·chinext 20。通过 → 滑点 buy×(1+10bp)、sell×(1−10bp)。先卖后买;买单 `scale_buys_to_cash` 等比缩放;apply_action 整百股,移动加权成本。

## 12. 费用

```
买入 fee = max(cost×0.0003, 5) + cost×0.00001
卖出 fee = max(proceeds×0.0003, 5) + proceeds×(stamp_duty(as_of)+0.00001)
卖出 realized = (price − avg_cost)×shares − fee
```

## 13. 估值

```
equity = cash + Σ(shares × price),price=close(qfq),当日无 close → last_close
```

## 14. 端到端示例

输入:某 ETF,T 日 20 日 ret=+4%,20 日 r²=0.65 且 slope>0,MA 多头排列。

```
momentum:         ret=4 → score=4×2=8.0
trend_linearity:  r2=0.65, slope>0 → score=0.65×20=13.0
trend_strength:   bullish → score=20.0
total = 8.0×1.0 + 13.0×0.6 + 20.0×0.4 = 8.0 + 7.8 + 8.0 = 23.8
final_signal = strong_buy(≥ 12)
target = round(0.05 × min(23.8/8, 1), 4) = 0.05
```

进 cap_and_rank 增仓池;若 running_gross 未超 0.95 → final=0.05 → 过去抖闸 → T+1 开盘买入。
