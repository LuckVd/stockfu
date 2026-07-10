# 回测引擎(`stockfu/backtest/` + `stockfu/ai/operators/`)

> 天级股票量化回测,**四层架构**:算子 → 策略 → 选股(rebalancer)→ 执行。
> 取数层严格防未来函数(所有 `as_of` 上界);算子结果全局缓存(`operator_result`),跨策略/跨回测复用。

## 1. 四层架构

```
算子 operator        一个独立信号源,输出 OpResult{signal, score, confidence, veto, value, ...}
   │                 两类:math(纯本地技术指标,7个)/ llm(4 顾问,复用 ai.analyze 链路)
   ▼
策略 strategy        一组算子 + 权重 + 聚合规则 + 去抖参数(YAML,存 strategy 表 + ai/strategies/*.yaml)
   │                 CompiledStrategy.analyze(code, as_of) → {context, opinions, aggregate, narrative}
   ▼
选股 rebalancer      跨标的看全集 → 每标的最终目标仓位(独立第三层,走 app_config)
   │                 pass_through(透传) / cap_and_rank(总仓位上限+排名) / top_n_picker(选 Top N+建仓锁定+限换手)
   ▼
执行 engine          T+1 开盘调仓 + VirtualAccount(移动加权+真实费用)+ 冷却/止损/边沿触发
                      → 绩效(总收益/年化/最大回撤/夏普/胜率/超额) + 权益曲线 + 交易明细
```

## 2. 怎么跑回测

### CLI(推荐)

```bash
python3 main.py --backtest bollinger_monthly \
    --start 2025-06-01 --end 2026-01-01 \
    --codes 600519,000858,600036 \
    --save
```

| 参数 | 说明 | 默认 |
|------|------|------|
| `--backtest STRATEGY` | 策略 ID(设 `app_config.active_strategy_id`) | 必填 |
| `--start` / `--end` | 区间 `YYYY-MM-DD` | 1 年前 → 今天 |
| `--cash` | 初始资金 | 1,000,000 |
| `--codes` | 逗号分隔代码;不传 = 全部 A 股自选 | 全量 |
| `--save` | 落盘到 `data/backtest/run-*.json`(**已 gitignore**,不入库) | 默认就存 |

入口链:`main.py --backtest` → `set_app_config(active_strategy_id)` → **`scheduler.run`**(注入 CompiledStrategy + temp=0 + 断点续跑缓存)→ `engine.run_backtest`。

输出示例:
```
回测 bollinger_monthly  2025-06-01 → 2025-07-01  初始资金 1,000,000  (10只票) …
✓ 总收益 6.56% | 年化 114.4% | 最大回撤 1.72% | 夏普 6.47 | 基准 N/A
  交易 6笔 | 期末权益 1065621.14
  结果已保存: data/backtest/run-YYYYMMDD-HHMMSS.json
```

### Python

```python
from stockfu.db import set_app_config
set_app_config("active_strategy_id", "bollinger_monthly")
from stockfu.ai.operators.registry import discover_and_register
discover_and_register()
from stockfu.backtest.scheduler import run

r = run(["600519", "000858"], "2025-06-01", "2025-07-01")
print(r["metrics"])          # {total_return, annualized, max_drawdown, sharpe, win_rate, benchmark_return, excess, ...}
print(r["trades"][:5])
print(r["equity_curve"][-1])
```

## 3. 策略(`strategy` 表 + `ai/strategies/*.yaml`,6 个)

| ID | 用到的算子 | 回测可用 |
|----|-----------|---------|
| `bollinger_monthly` | monthly_bollinger + momentum + trend_strength | ✅ 纯 math |
| `dual_bollinger` | weekly_bollinger + monthly_bollinger + momentum | ✅ 纯 math |
| `macd_cross` | macd_cross | ✅ 纯 math |
| `pure_factor` | momentum + mean_reversion + trend_strength + value | ✅ 纯 math |
| `hybrid` | valuation + trend + momentum + value + risk | ⚠️ 部分(含 LLM,需 key) |
| `classic_4advisors` | trend + contrarian + risk + valuation | ⚠️ 全 LLM,需 key |

切换策略:`set_app_config("active_strategy_id", "macd_cross")` 或 CLI `--backtest macd_cross`。

## 4. 算子(`operator` 表 + `ai/operators/`,13 个)

**Math 算子(7 个,`ai/operators/factors/`,全部支持 `as_of` 回测):**

| 算子 | 逻辑 | 关键参数 |
|------|------|---------|
| `monthly_bollinger` | 月线(≈21 日)布林带位置 | window, std_dev |
| `weekly_bollinger` | 周线(≈5 日)布林带位置 | window, std_dev |
| `momentum` | N 日涨跌幅动量 | window |
| `mean_reversion` | RSI 超买超卖 | rsi_period, oversold, overbought |
| `trend_strength` | MA5/10/20 多空头排列 | — |
| `value` | PE/PB 历史分位(读 `quote_snapshot.pe/pb`,`services.valuation.valuation_percentile`) | years |
| `macd_cross` | MACD 金叉/死叉 + 柱值 | fast, slow, signal |

**LLM 算子(4 个,`ai/operators/llm/advisors/`):** trend / contrarian / risk / valuation —— 即 `stockfu/ai/` 的 4 个常驻顾问(见 `docs/AI_ADVISORS.md`),作为算子注册后可在回测中按策略调用(需 LLM key)。

**聚合器(2 个,`ai/operators/aggregators/`,不缓存):** `weighted_sum`(加权求和)/ `risk_veto`(风险一票否决)。

## 5. 选股层 rebalancer(`ai/rebalancers/`,3 个)

通过 `app_config.active_rebalancer_id` 切换:

| 方案 | 逻辑 |
|------|------|
| `pass_through` | desired 原样透传(等价"无选股层") |
| `cap_and_rank` | 总仓位上限(`max_gross=0.95`)+ 增仓按 `score×confidence` 排序竞争额度 |
| `top_n_picker` | **每日全市场按 score 排名选 Top N(默认 10)+ 建仓锁定(20 日)+ Top10% 保护 + 限换手(每日 ≤2 只)** |

切换:`set_app_config("active_rebalancer_id", "cap_and_rank")`;参数:`set_app_config("rebalancer_params", '{"top_n": 10, "lock_days": 20, "max_replace": 2, "max_w": 0.20}')`。

## 6. 算子缓存(`operator_result` 表,核心加速)

每个 math 算子在 `(code, as_of, fingerprint)` 下只算一次,结果存 `operator_result` 表,后续任何策略/回测命中即读不重算:

- **fingerprint** = `sha1({version, params})`[:16](math)/ `sha1({version, prompt, temperature})`(llm)
- code/as_of 不进指纹(是表 key);prompt/params 改 → 指纹变 → 自动失效重算
- aggregator 不缓存(纯函数重算廉价)

**效果**:首次回测算 + 写缓存;同区间重跑 / 跨策略复用时秒级(直接读缓存)。首次大样本(如 788 票 × 全年)较慢是正常投入。

## 7. 执行模型(`engine.py`)

按**交易日**步进(akshare 交易日历;离线时自动 fallback 到 `quote_snapshot` 历史行情日):

1. **Phase 1**:T+1 开盘价执行前日挂单(停牌顺延,不丢弃)
2. **Phase 2**:`ThreadPoolExecutor` 并发跑 `analyze_fn`(`CompiledStrategy.analyze`,含算子+聚合+缓存)→ 信号
3. **Phase 3**:仓位层 `compute_target_weight`(信号→desired)+ risk 连续确认棒 + confidence gate → `rebalancer.adjust`(选股)→ `PositionManager` 边沿触发/冷却 → 挂单

**费用**(VirtualAccount,贴近真实):佣金万 3(最低 5 元/笔,双边)+ 印花税 0.05%(仅卖出)+ 过户费 0.001%(双边);A 股整百股。

**约束**:单股 `max_weight=0.15`、买入冷却 5 日、卖出冷却 3 日、总权益 `-3%` 止损。

## 8. 绩效指标口径(`_metrics`)

| 指标 | 口径 |
|------|------|
| `total_return` | (末值−初始)/初始 |
| `annualized` | `(末/初)^(252/天数)−1`(按交易日,口径准) |
| `max_drawdown` | 权益曲线峰值到谷值 |
| `sharpe` | 日收益均值 − 3%/252,÷ 日收益 std × √252 |
| `win_rate` | 盈利交易占比 |
| `benchmark_return` / `excess` | 沪深300 ETF(510300)对比;ETF 无历史数据时为 N/A |

## 9. 防未来函数(已验证)

- `services.factors.quote_series(code, field, days, as_of)` —— 查询带 `quote_date <= as_of` 上界
- `services.factors.ma_alignment(code, lookback, as_of)` —— 同上
- `services.valuation.valuation_percentile(code, as_of, years)` —— 读 `<=as_of` 序列算 PE/PB 分位
- `ai.context.build_context(code, as_of=None)` —— 三层情绪指数 + quote + 股息率全部 `<=as_of`;实盘调 `build_context(code)`(as_of=None=取最新),回测传 as_of
- engine 算子用 T-1 数据出信号,T+1 开盘执行

## 10. 待办

- benchmark 510300 ETF 在 `quote_snapshot` 无历史 → 基准常为 N/A(需回补宽基 ETF 行情)
- 拆表重构(ETF/指数行情独立成 `EtfQuoteDaily`/`IndexQuoteDaily`)尚未落地,当前 `quote_model_for` 单表路由(所有 code → `QuoteSnapshot`),拆表后此函数一处分流即可
