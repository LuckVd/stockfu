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
| `cap_and_rank` | 总仓位上限(`max_gross=0.90`)+ 增仓按**横截面百分位×confidence** 排序竞争额度 |
| `top_n_picker` | **每日全市场按横截面百分位(raw 连续强度)排名选 Top N(默认 10)+ 建仓锁定(20 日)+ Top10% 保护 + 限换手(每日 ≤2 只)** |

> **max_gross 总仓安全阀**(engine 层,本轮新增,对所有 rebalancer 生效,默认 0.90):Σ目标权重
> ≤ max_gross,留 1−max_gross 现金(cash sleeve)。优先级:`rebalancer_params.max_gross`
> > yaml `risk.max_gross` > 默认 0.90。配合执行层"先卖后买 + 买单等比缩放",保证买单总额
> ≤ 可投资现金、不夹断丢目标(详见 `docs/ARCHITECTURE_REVIEW.md`)。

> **为什么用横截面百分位而非 score**:score 的 ±20 clamp 让头部强势股集体撞顶、无区分度(实测首日 top10 曾只有 1 个不同值,边界票随进程抖动)。rebalancer 在 `adjust` 内对全市场 `raw`(`total_raw`)算当天百分位 [0,1] → 头部连续可分(top10 全不同),`code` 作最终 tiebreaker 保可复现。**纯截面操作**:只用 t 日各票 raw(均 `<=as_of`),不触碰 t+1 → 无未来函数。

切换:`set_app_config("active_rebalancer_id", "cap_and_rank")`;参数:`set_app_config("rebalancer_params", '{"top_n": 10, "lock_days": 20, "max_replace": 2, "max_w": 0.20}')`。

## 6. 算子缓存(`operator_result` 表,核心加速)

每个 math 算子在 `(code, as_of, fingerprint)` 下只算一次,结果存 `operator_result` 表,后续任何策略/回测命中即读不重算:

- **fingerprint** = `sha1({version, params})`[:16](math)/ `sha1({version, prompt, temperature})`(llm)
- code/as_of 不进指纹(是表 key);prompt/params 改 → 指纹变 → 自动失效重算
- aggregator 不缓存(纯函数重算廉价)
- `raw_score` 存在 detail JSON 字段;改算子 score 逻辑后需 bump version 或删旧缓存让其重算(否则旧缓存 raw=None 退化为 score)

**效果**:首次回测算 + 写缓存;同区间重跑 / 跨策略复用时秒级(直接读缓存)。首次大样本(如 788 票 × 全年)较慢是正常投入。

### 性能(G09:meta 缓存 + 索引策略 + WAL)

热缓存天级回测的三个加速杠杆(纯基础设施,**不改信号**——优化前后 metrics 逐值一致,已回归验证):

- **算子元信息进程级缓存**:`_load_operator_meta`(`runner.py`)挂 `@functools.lru_cache`,同进程内每个算子只查 1 次 `operator` 表;`CompiledStrategy._ensure_op_meta` 再做实例级缓存(每策略 + temperature 算一次)。砍掉旧的"每 (code, as_of, 算子) 一次 session 开闭"路径。
- **复合唯一键覆盖热路径**:`operator_result` 唯一复合索引 `uq_op_result_code_date_op_fp(asset_code, as_of, operator_id, fingerprint)` 覆盖全部查询点(全键等值 / `asset_code` 前导 IN / 单日批量预读);4 个单列索引已删(`models.py` 去 `index=True` + `db._migrate` 幂等 `DROP INDEX`)。`EXPLAIN QUERY PLAN` 均走复合索引,无全表扫。
- **WAL 模式**:`db.py` connect 监听器每连接设 `journal_mode=WAL` + `synchronous=NORMAL` + `busy_timeout=5000`。读不阻塞写(scheduler daemon 写入期回测读不 `SQLITE_BUSY`)、冷启动批量写缓存省 fsync。

> **WAL 备份 / 搬迁**:开 WAL 产生 `data/stockfu.db-wal` / `-shm` 旁路文件。备份或搬迁前先 checkpoint 把 -wal 并回主库,即可照旧单文件拷贝:
> ```bash
> python3 -c "import sqlite3;sqlite3.connect('data/stockfu.db').execute('PRAGMA wal_checkpoint(TRUNCATE)')"
> ```
> 或直接一并拷 `.db` + `.db-wal` + `.db-shm` 三文件。`python3 main.py --vacuum`(停 daemon/回测时跑)用 `VACUUM INTO` 原子重建主库回收空闲页(先备份 `.bak.G09`)。

## 7. 执行模型(`engine.py`)

按**交易日**步进(akshare 交易日历;离线时自动 fallback 到 `quote_snapshot` 历史行情日):

1. **Phase 1**:T+1 开盘价执行前日挂单(停牌顺延,不丢弃)
2. **Phase 2**:`ThreadPoolExecutor` 并发跑 `analyze_fn`(`CompiledStrategy.analyze`,含算子+聚合+缓存)→ 信号
3. **Phase 3**:仓位层 `compute_target_weight`(信号→desired)+ risk 连续确认棒 + confidence gate → `rebalancer.adjust`(选股)→ `PositionManager` 边沿触发/冷却 → 挂单

**费用**(VirtualAccount,贴近真实):佣金万 3(最低 5 元/笔,双边)+ 印花税 0.05%(仅卖出)+ 过户费 0.001%(双边);A 股整百股。

**资金分配 / 风控约束**(本轮对标 rqalpha/backtrader 升级,详见 `docs/ARCHITECTURE_REVIEW.md`):
- 单仓 `max_w=0.10`、总仓 `max_gross=0.90`(留 10% cash sleeve,对所有 rebalancer 生效)
- **先卖后买 + 买单等比缩放**(`engine.py` Phase1 + `cash_scaler.py`):卖单先释放现金,买单用 `safety` 标量等比缩放到可用现金(对标 rqalpha P 控制器),不逐笔 `min(delta,cash)` 夹断丢目标
- 个股成本止损 `stop_loss=8%`(浮亏→清仓)、组合回撤刹车 `portfolio_brake=10%`(→全局降仓一半);均可在 yaml `risk:` 段覆盖
- 买入冷却 5 日、卖出冷却 3 日(策略 debounce 可配)

## 8. 绩效指标口径(`_metrics`)

| 指标 | 口径 |
|------|------|
| `total_return` | (末值−初始)/初始 |
| `annualized` | `(末/初)^(252/天数)−1`(按交易日,口径准) |
| `max_drawdown` | 权益曲线峰值到谷值 |
| `sharpe` | 日收益均值 ÷ 日收益 std × √252(未减无风险利率;旧文档"−3%/252"为口径误) |
| `sortino` | 日收益均值 ÷ **下行**收益 std × √252(仅负收益计波动,本轮新增) |
| `calmar` | 年化收益 ÷ 最大回撤(本轮新增) |
| `avg/max_gross_leverage` | 平均/最大总仓位占比 %(从 holdings_curve 算,本轮新增) |
| `max_single_weight` | 最大单仓占比 %(实时,随股价漂移可超 max_w) |
| `cash_constraint_hits` | 买单被现金等比缩放的天数(可观测,对标 backtrader Margin) |
| `win_rate` | 盈利交易占比(按调仓动作;按交易回合统计见 P2-8) |
| `benchmark_return` / `excess` | 上证综指(sh000001,1990起);按交集区间算;无数据→None+reason |
| `benchmark_window` | 基准实际可用区间 `{start,end}` |

### 口径变更(2026-07-15:砍回测 LLM 算子 + 铲除 ±20/signal 体系)

- **score**:算子直出**连续值**(删 ±20 clamp;原 `raw_score` 并入 `score`)。各算子保留满强度刻度(momentum ±20=±10%涨幅 / bollinger 跌破下轨≈20 / value 低估≈20),但不硬截断 → 头部连续可分。
- **signal**:降级为**派生标签**(`score_to_signal` 从 total_score 派生,仅供展示/审计),**不参与仓位决策**。
- **仓位映射**:统一 **continuous 连续映射** `_total_to_weight(total/score_full → max_w)`;`score_full` 满仓刻度参数化(默认 20,策略 yaml `position.score_full` 可配,如 `macd_cross=10`)。discrete 模式 + `_SIGNAL_TARGET` 表已删,所有策略走 continuous。
- **OpResult**:删 `raw_score`/`evidence`/`tools_used`(剩 10 字段)。算子缓存指纹纳入**源码 hash**(`sha1(inspect.getsource(cls))`,治 P2-5:改算子代码自动失效旧缓存,不再依赖人工 bump version)。
- **回测 LLM 算子下线**(`operators/llm/` 整目录删);`hybrid`/`classic_4advisors` 策略废弃;active 默认改 `pure_factor`。**实盘 AI 4 顾问**(`ai/skills` 的 Opinion)独立链路保留,不受影响。
- **行为影响**:属行为改变类——同策略 metrics 不与旧基准逐值一致(连续映射替代 discrete 阶跃),但**确定性**(同参双跑全等)+ **防未来函数**(前缀一致性)红线均通过。

## 9. 时点宇宙 + 可成交(cn_large_pool_v1)

面向 **A 股大盘候选池(~800 只,`quote_snapshot`)** 的天级严谨回测;不做全 A / 消息面。

### 宇宙 `services/universe.py`

| 规则 | 默认 | 说明 |
|------|------|------|
| `universe_id` | `cn_large_pool_v1` | 基础池=库内个股;用户声明即选股宇宙 |
| `list_date` | 必用 | `security_master`(baostock 回补);`as_of < list_date` 不进截面 |
| `min_list_days` | 60 | 上市/首根 K 后冷静期(日历日) |
| `exclude_st` | True | 读 `quote_snapshot.is_st` |
| `require_trading` | True | `trade_status=0` 或无当日行情 → 不进新开仓截面 |
| 持仓例外 | — | 不在 U(t) 只减不加;ST 持仓目标 0 |

```bash
python3 main.py --backfill-universe          # 首次:拉 list_date/board
python3 main.py --backtest macd_cross --codes all --start 2025-06-01 --end 2025-08-01
python3 main.py --backtest macd_cross --codes 600519,000858 --no-strict   # 旧行为对照
```

`--codes`:省略=自选; `all`/`pool`=大盘候选;或逗号列表。每日再套 U(t)。  
产物 `metrics.config.universe` 含日均/最小/最大宇宙规模 + master 覆盖率。

### 可成交 `services/tradeability.py`

| 规则 | 默认 | 说明 |
|------|------|------|
| 涨跌停近似 | ON | `pct_chg` 近满幅度 + OHLC 粘合 → 涨停拒买 / 跌停拒卖(适配前复权) |
| 滑点 | 10 bps | 买贵卖便宜,略保守 |
| 停牌 | defer | 挂单顺延(与旧逻辑一致) |
| `--no-strict` | | 关涨跌停+滑点+宇宙收紧,便于 A/B |

指标:`limit_reject_buys` / `limit_reject_sells` / `fill_rejects` / `deferred_orders`。

### value 窗口

`value` 算子默认 **PE 5 年分位**(与 baostock 落库深度对齐);含 value 的策略建议 `start ≥ 2021-07`。

## 10. 防未来函数(已验证)

- `services.factors.quote_series(code, field, days, as_of)` —— 查询带 `quote_date <= as_of` 上界
- `services.factors.ma_alignment(code, lookback, as_of)` —— 同上
- `services.valuation.valuation_percentile(code, as_of, years)` —— 读 `<=as_of` 序列算 PE/PB 分位
- `ai.context.build_context(code, as_of=None)` —— 三层情绪指数 + quote + 股息率全部 `<=as_of`;实盘调 `build_context(code)`(as_of=None=取最新),回测传 as_of
- engine 算子用 T-1 数据出信号,T+1 开盘执行
- rebalancer 横截面百分位:只用 t 日 U(t) 内 score(均 `<=as_of`),纯截面操作不触碰 t+1
- 宇宙 `list_date` / 日 `is_st` / `trade_status` 只用 ≤as_of;涨跌停判决只用成交日 bar

### 端到端验证:前缀一致性测试(2026-07-14 实测,`macd_cross`)

look-ahead 最强检验——同一起点跑两次、终点不同,看"较早终点"那天的决策会不会被"较晚终点"影响:

- A: `[2025-06-01, 2025-07-01]`、B: `[2025-06-01, 2025-08-01]`,同一批 5 只票
- **先验确定性**:A 跑两次 → `equity_curve` / `trades` 逐笔逐点**完全一致**(temp=0 + as_of 键控缓存,引擎确定)
- **防泄漏**:B 在 `<=2025-07-01` 的前缀(21 个权益点 + 12 笔交易)与 A **逐字节一致**

把终点往后延一个月,7/01 及之前每一天的信号/仓位/目标权重/权益值分毫未变 —— 即引擎做 ≤7/01 决策时没"看到"8 月数据。**判定:无 look-ahead。**
实测口径(2 个月,`macd_cross`):总收益 -0.11% / 夏普 -0.01 / 最大回撤 3.2% / 胜率 60% / 12 笔交易(非虚高,对照修复前泄漏版的 +39%)。

## 11. 待办

- G02 / G09 / 时点宇宙+可成交(§9) 已落地。后续可选:流动性参与率、印花税历史档、未复权涨停价。

## 12. 因子诊断层（alphalens 思路，阶段2 / 2026-07-15）

单算子连续 `score` 独立量化为 IC / 分位收益 / 换手 / 衰减——**验证单个因子不必搭整条策略管道**（治"每只票每天必须跑全管道"冗长 + 补因子研究工作流缺口）。G10 铲除 ±20 后 score 连续可分，本层直接消费它。

### 怎么跑

```bash
python3 main.py --factor-diag momentum \
    --start 2025-01-02 --end 2025-06-30 \
    --codes all --periods 1,5,10,21 --quantiles 5 --save
```

| 参数 | 说明 | 默认 |
|------|------|------|
| `--factor-diag OPERATOR` | math 算子 ID（如 `momentum` / `macd_cross` / `value`） | 必填 |
| `--start` / `--end` | 信号区间 | 1 年前 → 今天 |
| `--codes` | 标的池；`all`/`stocks`/`market`=全市场个股(quote_snapshot 801 票)；逗号分隔=显式；不传=自选(Asset cn 52 票) | 自选 |
| `--periods` | 前向收益周期(交易日)，逗号分隔 | `1,5,10,21` |
| `--quantiles` | 分位桶数 | 5 |
| `--primary-period` | 分位收益/换手主周期(衰减 IC 表覆盖全部 periods) | 5 |
| `--params` | 算子参数 JSON（如 `'{"window":10}'`） | 算子 `PARAMS_SCHEMA` |
| `--save` | 落盘 `data/factor_diag/diag-*.json`（已 gitignore） | 不存 |

输出四件套（纯 Python，无 numpy/pandas 依赖，与 `_metrics` 同款）：

- **IC 衰减**：逐日横截面 Spearman(factor[t], forward_return[t]) → 序列统计（mean IC / IC IR=mean÷std / t-stat / 正 IC 占比 / 天数），按前向周期列表展示衰减结构。
- **分位收益**：按因子值横截面分 N 桶（Q1 最弱…QN 最强）算前向收益均值 + 多空价差(QN−Q1) + 单调性（桶号与均值的 Spearman）。
- **换手**：各分位组合日均成员变动率（对称差/并集），衡量持有该因子的换手成本。
- **样本规模**：标的池 / 信号日 / 因子观测数（透明可见，避免无意义小样本结论）。

### 关键设计

- **因子暴露 = 算子 `score`**（G10 后连续不 clamp），与 rebalancer 截面排名用同一个量；有效观测门槛 `value is not None`（各算子数据不足时 value=None）。
- **复用回测算子缓存**：因子面板走 `operator_result` read-through，指纹经 `single_operator_fingerprint`（version+params+source hash，与 `CompiledStrategy._ensure_op_meta` **逐字一致**）→ **回测算过的(code,as_of)因子诊断直接读、反之亦然**，跨场景互通。每日单日批量读缓存 + 并发算 miss + 单日批量落库（`save_operator_results_day`，一次 session），治大样本首跑逐行 session 慢。
- **防未来函数**：因子值在 `as_of=t` 算出（算子取数 `<=as_of`）；前向收益 `price[t+h]/price[t]-1` 用 t 之后价格——这是被预测对象、非泄露；IC 严格按日横截面算后再对日序列聚合（不跨日混池）。排名用平均秩 + code 兜底，确定性可复现。
- **确定性 + 跨工具缓存互通**均已回归验证（2026-07-15）：同参双跑 IC/分位/换手/衰减逐值一致；重跑 `miss=0`（全缓存命中）；factor-diag 写入的缓存行指纹与回测一致。

### 口径注

- 单日参与 IC 的最少标的数 `MIN_CROSS_SECTION=5`（不足该日不计）；前向收益按扩展交易日历位次推 h 日（跨停牌不漂移，仅当 t 与 t+h 两天都有收盘价才计）。
- A 股 momentum 实测（801 票，2025-05~06）：1/5 日 IC 为负（−0.038/−0.041，短线反转），分位单调性 −0.60——典型 A 股短周期动量反转特征，工具输出经济意义合理。
- **默认标的池 = 自选 52 票**（与回测一致，快速试）；**全市场因子研究用 `--codes all`**（801 票，首跑填缓存分钟级、一次性）。
