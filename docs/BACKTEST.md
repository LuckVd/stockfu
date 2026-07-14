# 回测引擎(`stockfu/backtest/` + `stockfu/ai/operators/`)

> 天级股票量化回测,**四层架构**:算子 → 策略 → 选股(rebalancer)→ 执行。
> 取数层严格防未来函数(所有 `as_of` 上界);算子结果全局缓存(`operator_result`),跨策略/跨回测复用。

## 1. 四层架构

```
算子 operator        一个独立信号源,输出 OpResult{signal, score(±20 语义), raw_score(连续排序), confidence, ...}
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
python3 main.py --backtest macd_cross \
    --start 2025-06-01 --end 2025-08-01 \
    --codes 600519,000858,600036,601318,002594 \
    --save
```

| 参数 | 说明 | 默认 |
|------|------|------|
| `--backtest STRATEGY` | 策略 ID(设 `app_config.active_strategy_id`) | 必填 |
| `--start` / `--end` | 区间 `YYYY-MM-DD` | 1 年前 → 今天 |
| `--cash` | 初始资金 | 1,000,000 |
| `--codes` | 逗号分隔代码;不传 = 全部 A 股自选 | 全量 |
| `--save` | 落盘 `data/backtest/{run_id}.json.gz` + `.meta.json`(**已 gitignore**,见下"产物结构") | 默认就存 |

入口链:`main.py --backtest` → `set_app_config(active_strategy_id)` → **`scheduler.run`**(注入 CompiledStrategy + temp=0 + 断点续跑缓存)→ `engine.run_backtest`。

输出示例(2026-07-14 实测,`macd_cross`):
```
回测 macd_cross  2025-06-01 → 2025-08-01  初始资金 1,000,000  (5只票) …
✓ 总收益 -0.11% | 年化 -0.64% | 最大回撤 3.2% | 夏普 -0.01 | 胜率 60.0% | 基准 N/A(ETF无数据)
  交易 12笔 | 期末权益 998878.89
  结果已保存: data/backtest/run-YYYYMMDD-HHMMSS.json.gz(+ .meta.json)
```

### 产物结构(`scheduler.py`)

每次 `run` 落**两个文件**到 `data/backtest/`(均 gitignore):

- **`{run_id}.json.gz`** —— 完整结果(gzip 压缩,约明文的 1/10):`equity_curve` / `holdings_curve`(逐日持仓快照)/ `trades`(含 pending 调仓意图,信号复盘用)/ `metrics` / `codes` / 策略身份(`strategy_id`/`operators`)+ `schema_version`。**原子写**(`.tmp`→`os.replace`),中断/崩溃不留半截损坏文件。
- **`{run_id}.meta.json`** —— 轻量摘要(几 KB):`metrics` / `codes` / 策略 / `data_size`。`list_runs()` **只扫 meta、不解析大产物**(实测列 13 个回测 1.97s → 1ms);`load_run()` 读完整 `.json.gz`,向后兼容旧明文 `.json`。

旧明文产物迁移:`python3 migrate_backtest.py`(幂等,`.json` → `.json.gz` + meta,实测 117M → 13M)。

### Python

```python
from stockfu.db import set_app_config
set_app_config("active_strategy_id", "macd_cross")  # 默认 active 是 bollinger_reversion;此处换 macd_cross(纯 math,已验证无未来函数)
from stockfu.ai.operators.registry import discover_and_register
discover_and_register()
from stockfu.backtest.scheduler import run

r = run(["600519", "000858"], "2025-06-01", "2025-07-01")
print(r["metrics"])          # {total_return, annualized, max_drawdown, sharpe, win_rate, benchmark_return, excess, ...}
print(r["trades"][:5])
print(r["equity_curve"][-1])
```

## 3. 策略(`strategy` 表 + `ai/strategies/*.yaml`,7 个)

| ID | 用到的算子 | 聚合 | 回测可用 |
|----|-----------|------|---------|
| `bollinger_reversion` ⭐ | daily_bollinger + weekly_bollinger + mean_reversion + trend_strength | weighted_sum | ✅ 纯 math(active 默认) |
| `momentum_breakout` | monthly_bollinger + momentum + trend_strength | weighted_sum | ✅ 纯 math |
| `dual_bollinger` | weekly_bollinger + monthly_bollinger + momentum | weighted_sum | ✅ 纯 math |
| `macd_cross` | macd_cross | weighted_sum | ✅ 纯 math(单算子,最快) |
| `pure_factor` | momentum + mean_reversion + trend_strength + value | weighted_sum | ✅ 纯 math |
| `hybrid` | valuation + trend + momentum + value + risk | risk_veto | ⚠️ 部分(含 LLM,需 key) |
| `classic_4advisors` | trend + contrarian + risk + valuation | risk_veto | ⚠️ 全 LLM,需 key |

切换策略:`set_app_config("active_strategy_id", "macd_cross")` 或 CLI `--backtest macd_cross`。

## 4. 算子(`operator` 表 + `ai/operators/`,14 个)

**Math 算子(8 个,`ai/operators/factors/`,全部支持 `as_of` 回测):**

| 算子 | 逻辑 | 关键参数 |
|------|------|---------|
| `monthly_bollinger` | 月线(≈21 日)布林带位置 | window, std_dev |
| `weekly_bollinger` | 周线(≈5 日)布林带位置 | window, std_dev |
| `daily_bollinger` | 日线布林带均值回归(中下轨买/中上轨卖,中轨死区) | window, std_dev, buy_max, sell_min |
| `momentum` | N 日涨跌幅动量 | window |
| `mean_reversion` | RSI 超买超卖 | rsi_period, oversold, overbought |
| `trend_strength` | MA5/10/20 多空头排列 | — |
| `value` | PE/PB 历史分位(读 `quote_snapshot.pe/pb`,`services.valuation.valuation_percentile`) | years |
| `macd_cross` | MACD 金叉/死叉 + 柱值 | fast, slow, signal |

> **score 双值**:每个算子输出 `score`(±20 clamp,语义分档 → signal)和 `raw_score`(clamp 前连续强度,排序用)。连续型(momentum / 三个 bollinger / mean_reversion / value)头部可分;离散型(trend_strength / macd_cross,交叉/排列是离散事件)raw=score。动机:±20 clamp 为对齐 LLM 量纲,却压平头部区分度 → rebalancer 排名走 raw(见 §5)。

**LLM 算子(4 个,`ai/operators/llm/advisors/`):** trend / contrarian / risk / valuation —— 即 `stockfu/ai/` 的 4 个常驻顾问(见 `docs/AI_ADVISORS.md`),作为算子注册后可在回测中按策略调用(需 LLM key)。

**聚合器(2 个,`ai/operators/aggregators/`,不缓存):** `weighted_sum`(同时聚合 `total_score`=Σ(score×w) 语义 + `total_raw`=Σ(raw×w) 排序)/ `risk_veto`(风险一票否决)。

## 5. 选股层 rebalancer(`ai/rebalancers/`,3 个)

通过 `app_config.active_rebalancer_id` 切换:

| 方案 | 逻辑 |
|------|------|
| `pass_through` | desired 原样透传(等价"无选股层") |
| `cap_and_rank` | 总仓位上限(`max_gross=0.95`)+ 增仓按**横截面百分位×confidence** 排序竞争额度 |
| `top_n_picker` | **每日全市场按横截面百分位(raw 连续强度)排名选 Top N(默认 10)+ 建仓锁定(20 日)+ Top10% 保护 + 限换手(每日 ≤2 只)** |

> **为什么用横截面百分位而非 score**:score 的 ±20 clamp 让头部强势股集体撞顶、无区分度(实测首日 top10 曾只有 1 个不同值,边界票随进程抖动)。rebalancer 在 `adjust` 内对全市场 `raw`(`total_raw`)算当天百分位 [0,1] → 头部连续可分(top10 全不同),`code` 作最终 tiebreaker 保可复现。**纯截面操作**:只用 t 日各票 raw(均 `<=as_of`),不触碰 t+1 → 无未来函数。

切换:`set_app_config("active_rebalancer_id", "cap_and_rank")`;参数:`set_app_config("rebalancer_params", '{"top_n": 10, "lock_days": 20, "max_replace": 2, "max_w": 0.20}')`。

## 6. 算子缓存(`operator_result` 表,核心加速)

每个 math 算子在 `(code, as_of, fingerprint)` 下只算一次,结果存 `operator_result` 表,后续任何策略/回测命中即读不重算:

- **fingerprint** = `sha1({version, params})`[:16](math)/ `sha1({version, prompt, temperature})`(llm)
- code/as_of 不进指纹(是表 key);prompt/params 改 → 指纹变 → 自动失效重算
- aggregator 不缓存(纯函数重算廉价)
- `raw_score` 存在 detail JSON 字段;改算子 score 逻辑后需 bump version 或删旧缓存让其重算(否则旧缓存 raw=None 退化为 score)

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
| `benchmark_return` / `excess` | 上证综指(sh000001,1990起);按交集区间算;无数据→None+reason |
| `benchmark_window` | 基准实际可用区间 `{start,end}` |

## 9. 防未来函数(已验证)

- `services.factors.quote_series(code, field, days, as_of)` —— 查询带 `quote_date <= as_of` 上界
- `services.factors.ma_alignment(code, lookback, as_of)` —— 同上
- `services.valuation.valuation_percentile(code, as_of, years)` —— 读 `<=as_of` 序列算 PE/PB 分位
- `ai.context.build_context(code, as_of=None)` —— 三层情绪指数 + quote + 股息率全部 `<=as_of`;实盘调 `build_context(code)`(as_of=None=取最新),回测传 as_of
- engine 算子用 T-1 数据出信号,T+1 开盘执行
- rebalancer 横截面百分位:只用 t 日全市场 raw(均 `<=as_of`),纯截面操作不触碰 t+1

### 端到端验证:前缀一致性测试(2026-07-14 实测,`macd_cross`)

look-ahead 最强检验——同一起点跑两次、终点不同,看"较早终点"那天的决策会不会被"较晚终点"影响:

- A: `[2025-06-01, 2025-07-01]`、B: `[2025-06-01, 2025-08-01]`,同一批 5 只票
- **先验确定性**:A 跑两次 → `equity_curve` / `trades` 逐笔逐点**完全一致**(temp=0 + as_of 键控缓存,引擎确定)
- **防泄漏**:B 在 `<=2025-07-01` 的前缀(21 个权益点 + 12 笔交易)与 A **逐字节一致**

把终点往后延一个月,7/01 及之前每一天的信号/仓位/目标权重/权益值分毫未变 —— 即引擎做 ≤7/01 决策时没"看到"8 月数据。**判定:无 look-ahead。**
实测口径(2 个月,`macd_cross`):总收益 -0.11% / 夏普 -0.01 / 最大回撤 3.2% / 胜率 60% / 12 笔交易(非虚高,对照修复前泄漏版的 +39%)。

## 10. 待办

- G02 已激活：基准从 510300 ETF 改为上证综指 `sh000001`（1990 起 8673 条，覆盖所有回测区间）。
  `_benchmark_curve` 直读 `IndexQuoteDaily` 表，不再经由 `quote_model_for`。
  `--backfill-benchmark` 一次性回补全历史；`run_scheduled_fetch` 每日自动追加（akshare 优先、baostock 兜底，多源 fallback）。
  基准窗口在 `metrics.benchmark_window` 可见；超额收益恒定产出。
