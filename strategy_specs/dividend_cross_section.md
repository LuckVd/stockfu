# dividend_cross_section 策略规格

对应配置:`stockfu/ai/strategies/dividend_cross_section.yaml`(含 `risk.stop_loss: 0.30`)。
本文档为策略机制的客观规格,不含回测绩效。参数来源:策略 YAML、rebalancer 运行配置、`stockfu/backtest/engine.py` 常量。

## 1. 标识

| 项 | 值 |
|---|---|
| strategy_id | dividend_cross_section |
| rebalancer | cap_and_rank |
| universe | cn_large_pool_v1 |
| 估值/成交价格口径 | 前复权 `*_qfq` |
| 股息率分母口径 | 不复权 `close_raw` |
| 基准 | sh000001(上证综指) |
| 初始资金 | 1,000,000 |

## 2. 配置参数总表

### 2.1 算子(operators)

| id | type | weight | params |
|---|---|---|---|
| dividend_yield | math | 1.0 | {high_yield: 5.0, price_basis: raw, yield_cap: 20.0} |
| low_volatility | math | 0.8 | {window: 20, hist_years: 3} |
| value | math | 0.6 | {years: 5} |

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
| stop_loss | 0.30 | yaml risk |
| portfolio_brake | 0.10 | 默认 DEFAULT_PORTFOLIO_BRAKE |
| edge_threshold | 0.005 | PositionManager 默认 |

### 2.6 执行(execution_rules)

| 参数 | 值 |
|---|---|
| execution | T+1_open_sell_first |
| strict | True |
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

### 2.8 宇宙规则(cn_large_pool_v1)

| 规则 | 值 |
|---|---|
| exclude_st | True |
| require_trading | True |
| min_list_days | 60(日历日) |
| use_list_date | True |
| min_amount_ma20 | 50,000,000(对当日成交额判定) |

## 3. 选股宇宙

基础池 = `quote_snapshot.asset_code` 去重(`resolve_base_codes("all")`)。

每个交易日 T,`eligible_on(T)` 输出可投资集 U(T),仅使用 ≤T 信息:

- `as_of ≥ delist_date` → 剔除
- anchor = `list_date`(use_list_date=True 时),否则该 code 在 quote_snapshot 的最小 quote_date;anchor 为空 → 剔除
- `as_of < anchor` → 剔除
- `(as_of - anchor).days < 60` → 剔除
- 当日 flags:
  - `is_st = True` → 剔除
  - 当日无行情行(`has_row = False`)→ 剔除
  - `trade_status = 0`(停牌)→ 剔除
  - 当日 `amount < 50,000,000` → 剔除

strict=True 时,U(T) 再交当日有 close 的票集合。

板块判定 `board_of_code(code)`:前缀 `688` → star;`300`/`301` → chinext;`8`/`4`(长度 6)→ bse;其余 → main。

## 4. 因子算子

每个交易日 T,对 U(T) 内每只 code 运行下列三算子,数据均 ≤T。每个算子输出 score、signal、confidence。

### 4.1 dividend_yield

输入 `dividend_yield_ttm(code, as_of)`:

- ttm = Σ per_share_cash,其中 ex_date ∈ [as_of − 365d, as_of]
- y = ttm / close_raw(as_of) × 100
- y = min(y, yield_cap = 20)

score 映射:

```
y ≥ 5.0        → score = 20.0,            signal = buy
1.0 ≤ y < 5.0  → score = 20×(y−1)/(5−1),  signal = buy if y≥3 else hold
y < 1.0        → score = 0,               signal = hold
```

无 TTM 分红或无 close_raw → score = 0, signal = hold, confidence = 0.3;有值 confidence = 0.6。

### 4.2 low_volatility

输入 `quote_series(code, "close", 3×365+20+30, as_of)`(qfq,共 1145 历日):

- rets[i] = closes[i] / closes[i−1] − 1
- std_series[k] = std(rets[k : k+20]),k ∈ [0, len(rets)−20]
- cur_std = std_series[−1]
- pct = percentile(std_series, cur_std)(时序百分位,0 = 序列内最低波动)

score 映射:

```
pct < 30  → score = 20×(1 − pct/30),               signal = buy
pct > 70  → score = −20×(1 − (100−pct)/30),        signal = sell
otherwise → score = 0,                             signal = hold
```

样本不足 / cur_std = 0 → score = 0, confidence = 0.3;有值 confidence = 0.6。

### 4.3 value

输入 `valuation_percentile(code, as_of, years=5)` → 近 5 年 PE 时序分位 pct:

```
pct < 20  → score = 20×(1 − pct/20),               signal = buy
pct > 80  → score = −20×(1 − (100−pct)/20),        signal = sell
otherwise → score = 0,                             signal = hold
```

样本不足 → score = 0, confidence = 0.3;有值 confidence = 0.6。

算子 score 取值范围:dividend_yield ∈ [0, 20];low_volatility ∈ [−20, 20];value ∈ [−20, 20]。

## 5. 聚合(weighted_sum)

```
total_score = round(dividend.score×1.0 + lowvol.score×0.8 + value.score×0.6, 2)
            ∈ [−28, +48]
confidence  = mean(三算子 confidence)
risk_vetoed = False
final_signal = score_to_signal(total_score, thresholds):
    ≥ 12   → strong_buy
    ≥ 4    → buy
    ≥ −4   → hold
    ≥ −12  → sell
    < −12  → strong_sell
```

final_signal 不参与仓位决策。

## 6. 仓位映射(compute_target_weight)

```
risk_vetoed → 0.0                                   # 本策略恒 False
否则 _total_to_weight(total_score, max_w=0.05, dead=3.0, score_full=8):
    total ≤ −3      → 0.0
    −3 < total < 3  → None                          # 死区,维持当前仓位
    total ≥ 3       → round(0.05 × min(total/8, 1.0), 4)
```

| total_score | 目标权重 |
|---|---|
| ≤ −3 | 0 |
| (−3, 3) | None |
| 3 | 0.0188 |
| 4 | 0.0250 |
| 6 | 0.0375 |
| ≥ 8 | 0.0500 |

## 7. 个股止损(stop_loss = 0.30)

仓位映射产出 target 后:

```
若 stop_loss_pct(0.30) > 0 且 current_weight > 0 且 target ∉ {0, None}:
    若 close(as_of) / avg_cost − 1 ≤ −0.30:
        target = 0
        signal = stop_loss
```

## 8. rebalancer(cap_and_rank)

输入 desired(code → target)、current(code → weight)、meta(code → {score, confidence, raw = total_score})、max_gross = 0.95。

```
pct[c] = _cross_section_percentiles(meta, desired∪current):
    按 meta[c].raw 升序排名,并列取平均秩,归一化到 [0,1]

遍历 desired ∪ current:
    desired = None      → final = None             (维持)
    desired ≤ current   → final = desired          (减仓/清仓,放行)
    desired > current   → 入增仓池;priority = pct[c] × confidence[c];final 暂占 current

running_gross = Σ final(已确定项,维持项取 current)
按 (−priority, code) 排序遍历增仓池:
    increment = desired − current
    若 running_gross + increment ≤ max_gross:
        final = desired;  running_gross += increment
    否则:
        final = current                                  (维持)
```

## 9. 组合层风控(rebalancer 之后)

### 9.1 组合刹车(portfolio_brake = 0.10)

```
peak = max(peak, equity)
若 equity / peak − 1 ≤ −0.10:
    final[c] = final[c] × 0.5                           (所有目标仓位减半)
```

### 9.2 总仓安全阀(_apply_gross_cap, max_gross = 0.95)

```
gross = Σ final[c](正值)
若 gross > max_gross:
    factor = max_gross / gross
    final[c] = final[c] × factor                        (正值等比缩放)
```

### 9.3 宇宙外持仓

final 中 code ∉ U(T) 且 final[code] > current[code] → final[code] = current[code](禁止加仓;减仓/清仓不受限)。
exclude_st 下,持仓当日 is_st → desired = 0(清仓)。

## 10. 去抖(PositionManager.should_act)

对 final 全集按 code 排序逐票判定:

```
max_target_step = 1.0:  若 target > executed → target = min(target, executed + 1.0)
min_w = max(0.005, 0.01):  若 |current − target| < min_w → 不下单
edge = 0.005:             若 |target − executed| < edge → 不下单
增仓(target > current):  buy_cool_down_days = 1,距上次买入 < 1 交易日 → 不下单
部分减仓(target < current 且 target > 0):  sell_cooldown_days = 1,距上次减仓 < 1 交易日 → 不下单
    清仓(target ≤ 0)不受冷却限制
通过 → pending_target[code] = target;更新 _last_executed / _last_buy / _last_sell
```

## 11. 执行(T+1 开盘)

T 日产生的 pending_target 在 T+1 日 Phase 1 执行。

### 11.1 成交价

```
price = open(T+1);  无 open → close(T+1);  均无 → 0(挂单顺延)
```

### 11.2 可成交检查(check_fill, strict)

按顺序判定,任一不通过即不成交(on_unfillable = defer → 挂单顺延次日):

```
price ≤ 0           → no_price
trade_status = 0    → suspended
limit_rule:
    pre_close = close / (1 + pct_chg/100)
    open_pct  = (open / pre_close − 1) × 100
    buy  且 open_pct ≥  limit_pct − 0.15  → limit_up_no_buy
    sell 且 open_pct ≤ −(limit_pct − 0.15) → limit_down_no_sell
    (无 pre_close 时回退:pct_chg 顶格 + OHLC 一字粘合判定)
limit_pct = limit_pct_for(board, is_st):
    is_st → 5;  star/chinext → 20;  bse → 30;  main → 10
通过 → apply_slip:
    buy  → price × (1 + 10/10000)
    sell → price × (1 − 10/10000)
```

### 11.3 成交顺序与缩放

```
1. 卖单(sell / reduce)先执行 → 释放现金
2. 买单(buy / add):scale_buys_to_cash 等比缩放
     若买单总额 > 可用现金 → 所有买单按 safety 标量等比缩放(constrained = True)
3. apply_action(code, target_weight, price):
     delta = target_value − current_value
     |delta| < total × 0.001 → 不动
     delta > 0(买):shares = int(min(delta, cash) / price / 100) × 100
         现金不足 100 股 且 pos = 0 且 cash ≥ 100×price + fee → shares = 100
     delta < 0(卖):shares = int(sell_value / price / 100) × 100,上限 = pos.shares
     整百股;成本 = 移动加权平均
```

## 12. 费用(apply_action)

```
买入 fee = max(cost × 0.0003, 5.0) + cost × 0.00001
卖出 fee = max(proceeds × 0.0003, 5.0) + proceeds × (stamp_duty(as_of) + 0.00001)
    stamp_duty(as_of) = 0.0005 if as_of ≥ 2023-08-28 else 0.001
卖出 realized = (price − avg_cost) × shares − fee
```

## 13. 估值

```
equity(as_of) = cash + Σ(positions[c].shares × price[c])
    price = close(qfq);  当日无 close → last_close(上一交易日 close)
equity_curve 记录每日收盘 equity。
```

dividend_event 仅作为 dividend_yield 算子输入;不向账户注入现金。

## 14. 端到端示例

输入:某 code,T 日

- TTM 股息率 y = 4.2(close_raw 分母)
- 20 日波动时序分位 pct = 15
- PE 5 年时序分位 pct = 25

算子:

```
dividend_yield:  y = 4.2 ∈ [1,5) → score = 20×(4.2−1)/4 = 16.0
low_volatility:  pct = 15 < 30  → score = 20×(1−15/30) = 10.0
value:           pct = 25        → score = 0
```

聚合:

```
total = 16.0×1.0 + 10.0×0.8 + 0×0.6 = 24.0
final_signal = strong_buy(≥ 12)
```

仓位映射:

```
total = 24 ≥ 8 → target = round(0.05 × min(24/8, 1), 4) = 0.05
```

进入 cap_and_rank 增仓池(priority = 横截面百分位(24.0) × 0.6);若 running_gross 未超 max_gross → final = 0.05 → 过去抖闸 → T+1 开盘买入(整百股,涨停检查,滑点 +10bp)。
