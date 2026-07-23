# momentum_breakout 策略规格

对应配置:`stockfu/ai/strategies/momentum_breakout.yaml`。
客观规格,不含绩效。

## 1. 标识

| 项 | 值 |
|---|---|
| strategy_id | momentum_breakout |
| rebalancer | top_n_picker |
| universe | universe_788(固定 788 只列表) |
| 估值/成交价格口径 | 前复权 `*_qfq` |
| 基准 | sh000001 |
| 初始资金 | 1,000,000 |

## 2. 配置参数总表

### 2.1 算子(operators)

| id | type | weight | params |
|---|---|---|---|
| monthly_bollinger | math | 1.2 | {window: 20, std_dev: 2.0} |
| momentum | math | 0.6 | {window: 20} |
| trend_strength | math | 0.4 | — |

### 2.2 聚合(aggregate)

- method: weighted_sum
- thresholds: {strong_buy: 10, buy: 4, hold: -4, sell: -10}

### 2.3 仓位(position)

| 参数 | 值 |
|---|---|
| mode | continuous |
| max_w | 0.10 |
| dead | 3.0 |
| score_full | 20(默认,未设) |

### 2.4 去抖(debounce)

| 参数 | 值 |
|---|---|
| buy_cool_down_days | 5 |
| sell_cooldown_days | 3 |
| max_target_step | 0.3 |
| risk_confirm_days | 1 |
| min_trade_weight | 0.01 |
| conf_gate | 0.3 |

### 2.5 风控与资金

| 参数 | 值 | 来源 |
|---|---|---|
| top_n | 10 | rebalancer_params(_TOP_N_MOM) |
| lock_days | 20 | rebalancer_params |
| max_replace | 1 | rebalancer_params |
| max_w(rebalancer) | 0.15 | rebalancer_params |
| max_gross | 0.90 | rebalancer_params |
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

universe_rules = None(strict=False)。

## 3. 选股宇宙

基础池 = `data/backtest/universe-788.txt`(固定 788 只)。每个交易日 T:

```
U(T) = { code | code ∈ 池 且 当日有 close }
```

(strict=False:engine 用 `set(close_prices.keys())`,无 ST/停牌/上市天数/成交额过滤。)板块判定:`688`→star;`300`/`301`→chinext;`8`/`4`(长度 6)→bse;其余→main。

## 4. 因子算子

### 4.1 monthly_bollinger

输入 `quote_series_dates(code, "close", 20×31+120, as_of)`(日线 qfq):

- 日线按月聚合(每月最后交易日 close)→ monthly_closes
- 月线布林:SMA(20) ± 2σ(对 monthly_closes[-20:])
- latest_close = 当日日线收盘;position = (latest_close − lower)/(upper − lower)

```
latest ≥ upper:  exceed=(latest−upper)/range; score = −8 − exceed×15;  sell/strong_sell
latest ≤ lower:  exceed=(lower−latest)/range; score =  8 + exceed×15;  buy/strong_buy
position < 0.3:  score = 6×(1 − position/0.3);                     buy
position > 0.7:  score = −6×((position−0.7)/0.3);                  sell
otherwise:       score = 0;                                        hold
```

月线数据 < 20 → score = 0, confidence = 0.3。命中置信度 0.4–0.8。

### 4.2 momentum

输入 `quote_series(code, "close", 50, as_of)`(qfq):

```
ret = (closes[−1]/closes[−20] − 1) × 100
score = ret × 2                      # 不 clamp
signal = buy if ret>3 else sell if ret<−3 else hold
confidence = 0.7
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
total_score = round(monthly_bollinger×1.2 + momentum×0.6 + trend_strength×0.4, 2)
confidence  = mean(三算子 confidence)
risk_vetoed = False
final_signal = score_to_signal(total_score, thresholds):
    ≥ 10 → strong_buy;  ≥ 4 → buy;  ≥ −4 → hold;  ≥ −10 → sell;  < −10 → strong_sell
```

final_signal 不参与仓位决策。

## 6. 仓位映射(compute_target_weight)

```
risk_vetoed → 0.0                                    # 恒 False
_total_to_weight(total_score, max_w=0.10, dead=3.0, score_full=20):
    total ≤ −3      → 0.0
    −3 < total < 3  → None
    total ≥ 3       → round(0.10 × min(total/20, 1.0), 4)
```

desired 供 top_n_picker 判定建仓符号(desired > 0 或 None = 看好);实际持仓权重见 §8(max_w = 0.15)。

| total_score | desired |
|---|---|
| ≤ −3 | 0 |
| (−3, 3) | None |
| 3 | 0.0150 |
| 10 | 0.0500 |
| ≥ 20 | 0.1000 |

## 7. 个股止损(stop_loss = 0.08)

```
若 current_weight > 0 且 desired ∉ {0, None}:
    若 close(as_of)/avg_cost − 1 ≤ −0.08 → desired = 0, signal = stop_loss
```

## 8. rebalancer(top_n_picker)

状态(回测全程同一实例):`_day` 交易日计数;`_entry_day[code]` 建仓时的 `_day`。每日 `_day += 1`。

```
① 排序:ranked = codes 按 (横截面百分位(raw)×confidence, code) 降序(risk_vetoed 沉底)
   target_set = ranked[:top_n=10]
② 持仓分类(held = current>0.001):
   risk_vetoed          → final=0, 清 entry
   in target_set        → final = max_w(0.15) if desired∈{None,>0} else desired
   非target 且 lock内    → final = current((_day−entry)<lock_days=20)
   非target 且 过lock    → 入 replaceable
③ 换仓(每日 ≤ max_replace=1):
   replaceable 按 rank 升序(排名低优先清);candidates = ranked 中 target 且未持仓(rank高优先建)
   n = min(max_replace, len(replaceable), len(candidates))
   清 replaceable[i]=0(清 entry);建 candidates[i]=max_w(entry=_day)
   未清 replaceable → 维持 current
④ 空仓首日:批量建 ranked[:top_n]=max_w(entry=_day)
⑤ 未覆盖:已持仓维持,其余 None
⑥ 硬约束:正值仓位数 ≤ top_n(超出清排名低且过lock的)
```

## 9. 组合层风控

- 组合刹车:peak = max(peak, equity);`equity/peak − 1 ≤ −0.10` → final[c] × 0.5。
- 总仓安全阀:Σ正值 > 0.90 → 正值等比缩放至 Σ=0.90。
- 宇宙外持仓:final[c] > current[c] → final[c] = current[c]。

## 10. 去抖(PositionManager.should_act)

按 code 排序逐票:`max_target_step=0.3`(target>executed → min(target, executed+0.3));`min_w=max(0.005,0.01)` 时 `|current−target|<min_w` 不下单;`edge=0.005` 时 `|target−executed|<edge` 不下单;增仓 `buy_cool_down_days=5`;部分减仓 `sell_cooldown_days=3`(清仓不限);`conf_gate=0.3`(弱 confidence 清仓降级为维持)。通过 → pending_target[code]=target。

## 11. 执行(T+1 开盘)

成交价 = open(T+1);无 open → close(T+1);均无 → 0(顺延)。check_fill:price≤0→no_price;trade_status=0→suspended;涨停(open_pct ≥ limit_pct−0.15)→拒买;跌停→拒卖;limit_pct = is_st 5 / star·chinext 20 / bse 30 / main 10。通过 → 滑点 buy×(1+10bp)、sell×(1−10bp)。先卖后买;买单 `scale_buys_to_cash` 等比缩放;apply_action 整百股,移动加权成本。

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

输入:某 code,T 日 monthly_bollinger position=0.20(<0.3),20 日 ret=+5%,MA 多头排列。

```
monthly_bollinger: position=0.20<0.3 → score=6×(1−0.20/0.3)=2.0
momentum:          ret=5 → score=10.0
trend_strength:    bullish → score=20.0
total = 2.0×1.2 + 10.0×0.6 + 20.0×0.4 = 16.4
desired = round(0.10 × min(16.4/20, 1), 4) = 0.0820  (>0,看好)
```

进 top_n_picker:若 code ∈ ranked[:10] → final = max_w = 0.15;若已持仓 lock 期内 → 维持;过 lock 且非 target → 入 replaceable。
