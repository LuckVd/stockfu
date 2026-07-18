# StockFu 回测架构评审与演进规划

> 对标业界开源量化框架(rqalpha / backtrader / zipline-reloaded / bt / PyPortfolioOpt)的架构评审。
> 结论:StockFu 四层架构方向正确且有差异化优势;软肋集中在**执行层资金分配**,本轮已修(P0+P1)。
> P2 较大改动单独立项,本文逐条记录**问题 / 原因 / 该怎么做 / 对标 / 定位**,供后续实施。

> **🔄 G10 后状态(2026-07-15)**:P2-5(算子指纹纳入源码 hash,治缓存失效坑)与 P2-6(清理 `operators/llm` 重复)已随 G10 完成——回测 LLM 算子整目录下线,算子数变为 **9 math + 2 聚合 = 11**(2026-07-16 加 trend_linearity 配 cn_momentum_rotation)(本文若干处仍写 13/7math/4llm,属历史记录,以本注为准;score 已连续不 clamp、signal 降级派生、统一 continuous 映射)。下阶段执行层演进走 P2-1/7→P2-8/4→P2-3/2(见 §8 顺序);另规划**阶段2 因子诊断层**(alphalens 思路,不在本文 P2 清单,见 `PROJECT_STATE.md` §8)。

---

## 1. 对标框架速览

| 框架 | 定位 | 对 StockFu 的启发点 |
|---|---|---|
| **rqalpha** | A股事件驱动回测(米筐) | `order_target_portfolio_smart` P 控制器全局缩放买单;现金三态;T+1 `_non_closable` |
| **backtrader** | 分层架构标杆 | Strategy/Sizer/Broker/Analyzer/CommissionInfo 解耦;现金不足=Margin 拒绝(可观测) |
| **zipline-reloaded** | 目标权重范式 | Pipeline 编译型计算图(横截面向量化 + refcount GC);放透支+leverage 计量 |
| **bt** | 可组合 algo 栈 | 时机→选择→权重→执行 可组合;树状策略组合 |
| **PyPortfolioOpt** | 凸优化组合构造 | `discrete_allocation.py` 连续权重→整手股数;约束求解 |

---

## 2. 总体结论

**架构方向是对的。** 四层(算子→策略→rebalancer→执行)对应业界 signal→portfolio construction→execution 三层,且在以下 3 点**领先**通用开源框架:

1. **算子结果持久缓存 `operator_result`**(`stockfu/ai/operator_cache.py`):LLM 算子单次比 numpy 因子贵 6 个数量级,持久缓存 + 单日批量预读是刚需;rqalpha/zipline/backtrader **都没有**(zipline 只有 run 内 WeakValueDictionary 去重)。
2. **显式 rebalancer 组合层**(`stockfu/ai/rebalancers/`):把"跨标的组合构造"独立成可插拔层;backtrader/zipline 的组合约束要用户在策略代码里手写。
3. **anti-whipsaw 成体系 + T+1 整百股 A股本地化**:`PositionManager` 边沿触发/冷却/滞回死区;真实三费(佣/印/过)。backtrader/zipline 默认都要用户造轮子。

**软肋高度集中在执行层资金分配** —— 即本轮 P0 修复的核心:`min(delta,cash)` 静默夹断丢目标、买卖单按 code 字典序混排(非先卖后买)、默认不限总仓。

---

## 3. 已实施(P0 + P1,本轮)

### 3.1 资金分配主线(P0)

| 改动 | 文件 | 说明 |
|---|---|---|
| **先卖后买 + 买单等比缩放** | `stockfu/backtest/engine.py` Phase1 | 卖单先成交释放现金;买单用 `safety` 标量等比缩放到可用现金(对标 rqalpha P 控制器),不逐笔夹断丢目标 |
| **现金缩放器** | `stockfu/backtest/cash_scaler.py`(新) | `scale_buys_to_cash`:Σ(买单成本)+估算费用 ≤ 现金,超限等比缩放增量;费用纳入 |
| **总仓安全阀 `max_gross`** | `engine.py` `_apply_gross_cap` | rebalancer 后施加,Σ目标 ≤ max_gross(默认 0.90,留 10% cash sleeve),对所有 rebalancer 生效 |
| **修 cash 变负 bug** | `engine.py` `apply_action` 建仓特例 | 预检纳入手续费(旧版只判 price×100,扣费后 cash 落到约 -5) |
| **max_w 0.15/0.20 → 0.10** | `strategies/*.yaml` + DB `strategy.config` | 单仓占总资产上限 10%(用户要求) |
| **清理死代码/死参数** | `bollinger_reversion`/`momentum_breakout` 删 continuous 无效 targets;seed `rebalancer_params` 对齐 | continuous 模式 targets 不生效;pass_through 下旧 params 无效 |
| **夹断透明化** | `engine.py` | `cash_constraint_hits` 指标 + trade 记 `cash_scaled` safety |

### 3.2 风控/绩效(P1)

| 改动 | 文件 | 说明 |
|---|---|---|
| **个股成本止损** | `engine.py` Phase3a | `stop_loss_pct`(默认 8%)浮亏→清仓;补 BACKTEST.md 承诺但缺失的代码(旧文档写"-3%"对 A股太敏感) |
| **组合回撤刹车** | `engine.py` Phase3b | `portfolio_brake_dd`(默认 10%)equity 较峰值回撤→全局降仓一半 |
| **指标增强** | `engine.py` `_metrics` | 新增 sortino / calmar / avg&max gross_leverage / max_single_weight / cash_constraint_hits |
| **风控可配** | `runner.py` `StrategyDebounce` + `risk:` 段 | yaml `risk: {max_gross, stop_loss, portfolio_brake}` 可覆盖 engine 默认 |

### 3.3 验证(2026-07-15)

- **确定性**:macd_cross 同参两次 `metrics` 全等(temp=0 + as_of 键控缓存 + code 排序)。
- **cash 三场景不负**:macd_cross(min 624261)、bollinger_reversion(min 6375)、top_n_picker 极端 1 万资金(min 115.9)。
- **缩放逻辑**:`scale_buys_to_cash` 单测等比缩放正确(safety=0.833);集成极端场景 `cash_constraint_hits=9` 确认可达。
- **max_gross 生效**:默认场景 gross_leverage ≤ 90%。

---

## 4. P2 待实施(逐条:问题 / 原因 / 怎么做 / 对标 / 定位 / 工作量)

> 本轮不做,单独立项。按性价比排序。

### P2-1. math 算子向量化 `run_batch`(治冷启动慢)
- **问题**:math 算子逐 (code,as_of) 标量计算,冷启动(缓存空)是 `O(N票×M算子×D天)` 次 Python 调用 + 各自查 quote 序列;zipline 是 `O(D)` 次矩阵运算。
- **原因**:`operators/base.py:73` 的 `run_batch` 接口**留好但未实现**(默认 raise)。math 算子本可向量化却走了逐点路径。
- **怎么做**:借鉴 `zipline/pipeline/factors/technical.py` —— 每个 math 算子实现 `run_batch(codes, as_of_list, params)`,一次 SQL 取 `(codes×dates)` 收盘价矩阵,numpy/numexpr 算完全集,批量回填 `operator_result`。LLM 算子保持逐点 + 持久缓存(天然不可向量化)。
- **对标**:zipline Pipeline 横截面向量化。
- **定位**:`stockfu/ai/operators/base.py:73`、`factors/*.py`、`operator_cache.py:get_operator_results_batch`(批量读已做,批量写待补)。
- **工作量**:中(每个 math 算子重写 run_batch,8 个)。属 G09 回测性能范畴。
- **注意**:向量化结果必须与逐点逐字节一致(否则缓存指纹需 bump),建议保留逐点作 oracle 对拍。

### P2-2. Broker 抽象(回测/实盘共用执行层)
- **问题**:`VirtualAccount` 把"账户状态 + 撮合 + 费用 + 整百股规则"全揉在一个类,且只在回测用。CLAUDE.md 承诺"回测与实盘共用同一决策层",但**执行层没共用**(决策层 PositionManager/rebalancer 共用了)。
- **原因**:执行层没有抽象接口,实盘接入要重写撮合。
- **怎么做**:对标 backtrader `BrokerBase`(`buy/sell/getcash/getvalue/getposition`)+ `BackBroker`/`LiveBroker`。把 `VirtualAccount` 拆成 `BrokerBase`(接口)+ `BackBroker`(回测撮合),实盘 `LiveBroker` 包装券商 API。
- **对标**:backtrader `backtrader/brokers/{bbroker,ibbroker}.py` 共用 `BrokerBase`。
- **定位**:`stockfu/backtest/engine.py:40` `VirtualAccount`。
- **工作量**:中偏大(拆类 + 实盘 Broker 需对应券商 API)。

### P2-3. Sizer / CommissionInfo 抽象(费用/整手数可替换)

> **进度(2026-07-18)**:第一步「印花税日期化」已落地——`stamp_duty_rate(as_of)`(2023-08-28 前千一 0.001 / 后万五 0.0005)接入 `VirtualAccount.apply_action`(加 `as_of` 形参)+ probe 的 `NotionalAccount`,治跨历史区间失真;§7 基准窗口全在 08-28 后故逐值不变(可证明零行为改变)。过户费日期化(2022 沪深统一)、CommInfo/Sizer 抽象仍待做。

- **问题**:费用率(`COMMISSION_RATE` 等)和整百股规则**硬编码为模块常量**(`engine.py:27-30`)。A股费率历史调整(如印花税 2023-08 降税率千一→万五,**已日期化**;过户费 2022 沪深统一仍未日期化),跨历史区间回测会失真;不同品种(股/ETF/港股)整手数不同(港股 1 手非 100)。
- **原因**:没有可替换的费用/合约规格对象。
- **怎么做**:对标 backtrader `CommissionInfo`(`getsize/getcommission/profitandloss`,stocklike/futures、perc/fixed)+ `Sizer`(`_getsizing(comminfo,cash,data,isbuy)→int`)。把 `apply_action` 里整百股 + 费用计算抽成 `CommInfo`/`Sizer` 对象,按 code 绑定不同规格。
- **对标**:backtrader `comminfo.py`、`sizer.py`;PyPortfolioOpt `discrete_allocation.py`(连续权重→整手)。
- **定位**:`engine.py:27-30`(费率)、`:84/:101`(整百股 `int(.../100)*100`)。
- **工作量**:中。顺带消除硬编码,跨品种/跨区间可挂不同 CommInfo。

### P2-4. Analyzer 可组合体系(_metrics 拆分)
- **问题**:`_metrics`(`engine.py`)是单函数,所有指标挤一起;加指标要改这个函数;无按年/月分段;trade 分析 win_rate 按"调仓动作"非"交易回合"(开仓→平仓配对),统计意义弱。
- **原因**:没有可插拔 Analyzer 抽象。
- **怎么做**:对标 backtrader `Analyzer`(`start/next/stop/notify_*` + `get_analysis()`,**组合而非继承**——Sharpe 内嵌 TimeReturn)。拆成 Sharpe/DrawDown/AnnualReturn/TradeAnalyzer/Calmar,`run_backtest` 主循环每日调 `next`、结尾 `stop` 汇总。
- **对标**:backtrader `analyzer.py` + `analyzers/sharpe.py`(组合模式)。
- **定位**:`engine.py:_metrics`(已加 sortino/calmar/gross_leverage,但仍单函数)。
- **工作量**:中。本规模可能 over-engineered,择优抄几个 + TradeAnalyzer(需 P2-8)。

### P2-5. 算子 version 机制化(治指纹坑)
- **问题**:`Operator.version` 靠人手维护,改算子逻辑不 bump version → 旧缓存命中 → 回测用旧代码(MEMORY 已记 `operator-cache-staleness`)。
- **原因**:指纹只含 `(version, params)`,不含源码;version 是人工字段。
- **怎么做**:两选一 —— (a) 指纹纳入算子源码 hash(`inspect.getsource(cls)`),改代码自动失效缓存;(b) CI 检查"算子源码 git diff 但 version 未变"就报警。zipline 没这问题(不持久缓存),但 StockFu 持久化了就必须机制化。
- **对标**:无直接对标(zipline 不持久缓存);理念上对标软件缓存失效。
- **定位**:`operator_cache.py:compute_fingerprint`、`operators/seed.py:_load_operator_meta`、`models.py:Operator.version`。
- **工作量**:小。(a) 最省事,`inspect.getsource` 加进指纹即可。

### P2-6. 清理 `ai/operators/llm/` 与 `ai/skills/` 重复
- **问题**:两套近乎重复的 tools/advisors 实现(tools 差 6-10 行,advisors 差 4-19 行)。实盘链路(`analyze.py`/`context.py`/`synthesis.py`)**实际 import `skills/`**;`operators/llm/` 仅 `seed.py:57` 引了 `ALL_ADVISORS`。
- **原因**:历史重构残留——LLM 算子从 `skills/` 迁到 `operators/llm/` 但没删旧,或反之。两套并存导致维护/认知负担。
- **怎么做**:确认哪个是"活的"(skills/ 被 analyze 链路用),把 `operators/llm/{tools,advisors}` 删除或改为 re-export `skills/` 的符号;`seed.py:57` 改引 `skills/advisors`。跑回归确认 analyze 链路不变。
- **对标**:无(纯代码卫生)。
- **定位**:`stockfu/ai/operators/llm/{tools,advisors}/` vs `stockfu/ai/skills/{tools,advisors}/`;`seed.py:57`。
- **工作量**:小(删重复 + 改 import + 回归)。需先确认 seed 的 `ALL_ADVISORS` 来源与 skills 是否同构。

### P2-7. 轻量计算图(共享原始输入加载)
- **问题**:多个 math 算子各自独立调 `quote_series`/`build_context`,若两个算子都要 60 日收盘序列,就查两遍 DB。
- **原因**:算子间无共享输入机制(`OpContext.factors` 只让 LLM 读 math 结果,math↔math 不共享原始序列)。
- **怎么做**:不必上 zipline 完整 TermGraph+refcount,加"算子输入声明":每个 math 算子声明 `required_series=["close_60d","volume_20d"]`,`CompiledStrategy.analyze` 跑算子前**一次性**批量取出放进 `OpContext.series`(`base.py:28` 已预留字段!),算子从 ctx 取而非各自查库。
- **对标**:zipline Pipeline "loader 批量取数" 的轻量化。
- **定位**:`operators/base.py:27` `OpContext.series`(预留)、`runner.py:149` 算子执行循环。
- **工作量**:小到中。和 P2-1 向量化可协同(向量化天然共享矩阵)。

### P2-8. Position opened/closed 分解(交易回合统计)
- **问题**:`Position` 只有 `shares/avg_cost`(`engine.py:34`),无法区分"这次成交里多少开仓/多少平仓",无法把"分批建仓→分批减仓"配对成独立 Trade。所以 win_rate 按"调仓动作"算,非"交易回合",统计意义弱。
- **原因**:`apply_action` 卖出分支手算 realized pnl,但不维护 opened/closed。
- **怎么做**:`Position.update` 返回 `(size,price,opened,closed)`(对标 backtrader `position.py`),配对开平成 Trade,算持有期/单笔盈亏/连续盈亏。配合 P2-4 TradeAnalyzer。
- **对标**:backtrader `position.py:Position.update`;rqalpha `position_model.py`。
- **定位**:`engine.py:34-38` `Position`、`:99-115` 卖出分支、`:506` win_rate。
- **工作量**:中。顺带加 T+1 `_non_closable`(对标 rqalpha,当前 T+1 开盘设计下优先级低)。

### P2-9. 文档与代码对齐
- **问题**:多处文档过时 —— BACKTEST.md §7"-3%止损"(代码本无,本轮已补参数化版)、§8 sharpe"减 3%/252"(代码实际不减,本轮已更正);算子数"13(7math)"(实际 14=8math+4llm+2聚合)。本轮已更正 BACKTEST.md 关键处。
- **怎么做**:把文档校验纳入 CI(grep 关键数值与代码常量对拍),或文档从代码常量生成。
- **工作量**:小。

---

## 5. 关键设计决策记录:现金受限的四哲学

"某日买单总额 > 可用现金"时,业界四种做法:

| 框架 | 做法 | 丢目标? | 可观测? |
|---|---|---|---|
| rqalpha v6 | P 控制器 `safety` 标量全局等比缩放买单 | 否 | 误差全程可见 |
| zipline | 允许 cash 变负(透支)+ leverage 指标 + `set_max_leverage` 熔断 | 否(透支) | leverage 可见 |
| backtrader | `Margin` 整单拒绝 + `notify_order` | 是(需次日补) | Rejected 可见 |
| **StockFu(本轮)** | **max_gross 留 cash sleeve + 先卖后买 + 买单等比缩放**(rqalpha 式) | **否** | `cash_constraint_hits` + `cash_scaled` |

**为什么选 rqalpha 式等比缩放而非 backtrader Margin 拒绝**:等比缩放不丢目标(所有买单按同比例分摊缺口)、与执行序无关;Margin 拒绝会丢目标需次日补,且高频触发时持仓长期偏离。配合 `max_gross` 留 cash sleeve,**常态下买单总额已 ≤ 可投资现金,缩放只在整百股/费用/T+1跳价的边界情况触发**(实测正常策略 `cash_constraint_hits=0`,极端满仓小资金才 9)。

**为什么选 max_gross 留现金而非 zipline 放透支**:透支=放杠杆,会让回测收益虚高且不真实;留 cash sleeve 把现金当正式资产类别(AQR "cash drag" 理论),更贴近真实券商场内资金。

---

## 6. P2 冷启动入口与验证规程

### 6.1 通用冷启动入口(任何 P2 先读)
做任何 P2 前先读这几份建立上下文:
- `docs/BACKTEST.md`(四层架构 + 执行模型 + §9 防未来函数前缀一致性测试)
- `docs/ARCHITECTURE_REVIEW.md`(本文:对标结论 + P2 清单 + 本节入口/验证)
- `docs/PROJECT_STATE.md`(数据现状 + 已知坑)
- `stockfu/backtest/engine.py`(执行层全貌,Phase1/3 + apply_action + _metrics)
- `stockfu/ai/operators/runner.py`(策略编译 + 算子执行链)
- 对应 P2 的"定位"文件(见 6.2)

### 6.2 每条 P2 的必读文件清单
| P2 | 必读文件(定位) |
|---|---|
| P2-1 向量化 | `operators/base.py:73`(run_batch 接口)、`factors/*.py`(逐点实现,作 oracle)、`operator_cache.py`(get_operator_results_batch 批量读 + 写路径)、`runner.py:137`(算子执行循环) |
| P2-2 Broker | `engine.py:40` VirtualAccount、`engine.py` Phase1 调用方、`scheduler.py`/`main.py:155` 入口 |
| P2-3 Sizer/CommInfo | `engine.py:27-30`(费率常量)、`:84`/`:101`(整百股)、`apply_action` 全貌 |
| P2-4 Analyzer | `engine.py` `_metrics` + 结尾 metrics 组装、`holdings_curve`/`equity_curve` 产出 |
| P2-5 version | `operator_cache.py:compute_fingerprint`、`seed.py:_load_operator_meta`、`models.py:Operator.version` |
| P2-6 清理重复 | `ai/operators/llm/{tools,advisors}/`、`ai/skills/{tools,advisors}/`、`seed.py:57`、`analyze.py`/`context.py`/`synthesis.py`(确认 import skills) |
| P2-7 计算图 | `operators/base.py:27` OpContext.series(预留)、`runner.py` 算子循环、各算子查 `quote_series` 处 |
| P2-8 Position | `engine.py:34` Position、`:99-115`(卖出分支)、`:506`(win_rate) |
| P2-9 文档对齐 | `BACKTEST.md`/`CLAUDE.md`/`PROJECT_STATE.md` vs 代码常量 |

### 6.3 验证 / 回归规程(每条 P2 改完必须过)
1. **AST + import**:`python3 -c "from stockfu.backtest import engine,scheduler; from stockfu.ai.operators import runner"`
2. **回归基准对拍**(行为不变的 P2: P2-1/5/6/7/9):跑 §7 基准命令,metrics 必须**逐值一致**。这是"纯重构/加速不应改变结果"的硬门槛。
3. **确定性**(改了执行/撮合的 P2: P2-2/3/8):同参跑两次 `r1["metrics"]==r2["metrics"]` 全等。
4. **前缀一致性**(改了取数/算子的 P2: P2-1/5/7):按 BACKTEST.md §9,A=`[start,mid]` B=`[start,end]`,B 在 ≤mid 的前缀(equity_curve + trades)与 A 逐字节一致 —— 防未来函数回归。
5. **现金不负**(改了执行层的 P2: P2-2/3/8):`min(d["cash"] for d in holdings_curve) >= -0.01`。
6. 端到端用 `/verify` skill 驱动确认。

**行为改变的 P2**(P2-2 改撮合语义、P2-4 改指标口径、P2-8 改 win_rate 算法):不能要求 metrics 与旧基准一致;改为**单元测试新逻辑 + 在 BACKTEST.md §8 显式记录口径变更 + 给出前/后 metrics 对比**。

---

## 7. 回归基准(行为不变类 P2 的对拍锚点)

跑此命令,metrics 应与下表**逐值一致**(否则行为回归):
```bash
cd /opt/pro/stockfu && python3 << 'EOF'
from stockfu.db import set_app_config
set_app_config("active_strategy_id","macd_cross")
set_app_config("active_rebalancer_id","pass_through")
from stockfu.ai.operators.registry import discover_and_register; discover_and_register()
from stockfu.backtest.scheduler import run
r=run(["600519","000858","600036","601318","002594"],"2025-06-01","2025-08-01")
m=r["metrics"]
for k in ["total_return","annualized","max_drawdown","sharpe","sortino","calmar","win_rate",
          "trade_count","avg_gross_leverage","max_gross_leverage","max_single_weight",
          "cash_constraint_hits","total_fee","final_equity"]:
    print(f"{k}={m[k]}")
EOF
```

| 指标 | 基准值(2026-07-18 重锚;strict 宇宙默认 + 数据更新后) |
|---|---|
| total_return | 0.04 |
| annualized | 0.22 |
| max_drawdown | 1.05 |
| sharpe | 0.11 |
| sortino | 0.08 |
| calmar | 0.21 |
| win_rate | 60.0 |
| trade_count | 13 |
| avg_gross_leverage | 12.2 |
| max_gross_leverage | 20.8 |
| max_single_weight | 14.2 |
| cash_constraint_hits | 0 |
| total_fee | 177.8 |
| final_equity | 1000384.23 |

> 基准锚定当前实现(max_w=0.10 / max_gross=0.90 / 先卖后买 / 买单缩放 / strict 宇宙默认)。2026-07-18 重锚:07-15 后 strict 时点宇宙(§9)+ 数据刷新落地,metrics 较 07-15 表漂移(total_fee 532.88→177.8 等),属行为演进非回归。若某 P2 同时改了这些(如 P2-3 调费率),需同步更新本表。

---

## 8. 跨任务依赖与建议顺序

```
P2-5 (version 机制化) ──┐ 独立、最小,治已知缓存坑
P2-9 (文档对齐)      ──┤ 独立、机械
P2-6 (清理重复)      ──┘ 独立、小
        │  ① 先清债(小而独立)
        ▼
P2-1 (math 向量化) ◄──┐ 协同:向量化天然共享输入矩阵
P2-7 (轻量计算图)  ──┘   ② 性能(属 G09)
        │
        ▼
P2-8 (Position opened/closed) ──► P2-4 (Analyzer/TradeAnalyzer)   ③ 统计增强
        │
        ▼
P2-3 (Sizer/CommInfo) ──► P2-2 (Broker 抽象,含 Sizer)   ④ 执行层抽象,为实盘铺路
```

建议顺序:**① P2-5/9/6(清债)→ ② P2-1+P2-7(性能)→ ③ P2-8→P2-4(统计)→ ④ P2-3→P2-2(执行层抽象)**。

---

## 9. 接口契约草案(较大项,冷启动可直接按此动手)

### 9.1 P2-1 math 算子 `run_batch`
```python
class BaseOperator:
    def run(self, ctx, params) -> OpResult: ...   # 现有逐点实现,保留作 oracle
    def run_batch(self, codes: list[str], as_of_list: list[date],
                  params: dict) -> dict[tuple[str, date], OpResult]:
        """向量化:一次取 (codes × as_of) 收盘价矩阵,numpy/numexpr 算全集,返回多结果。
        默认 raise(未实现,见 base.py:73)。实现后 runner 优先调 run_batch;
        结果必须与逐点 run 逐字节一致(对拍:同 inputs 两条路径 OpResult 全等)。"""
```
- 实现步骤:① 选 1 个算子(如 `momentum`)写 `run_batch` + 对拍脚本(逐点 vs 批量,OpResult 全等);② 通过后批量回填 `operator_result`(复用 `get_operator_results_batch` 的写路径);③ 缓存命中读路径不变,只是 miss 时走批量算。
- 回归门槛:§7 macd_cross 基准逐值一致(向量化不应改变结果)。

### 9.2 P2-2 Broker 抽象
```python
class BrokerBase(Protocol):
    cash: float
    positions: dict[str, Position]
    def equity(self, prices) -> float: ...
    def weight(self, code, prices) -> float: ...
    def apply_action(self, code, action, target_weight, price, prices) -> dict | None: ...
# BackBroker = 现 VirtualAccount 的撮合逻辑(改名,零行为改变)
# LiveBroker = 包装券商 API(实盘,本轮不做,单独立项)
```
- 第一步纯重命名:`VirtualAccount` → `BackBroker(BrokerBase)`,**零行为改变**,§7 基准必须逐值一致。
- `engine.run_backtest` 加 `broker: BrokerBase` 参数(默认 `BackBroker(initial_cash)`);Phase1 的 `acct.*` 调用走接口。
- LiveBroker 不在本轮范围。

### 9.3 P2-4 Analyzer 可组合(组合,非继承)
```python
class Analyzer:
    def start(self, initial: float) -> None: ...
    def next(self, as_of: date, equity: float, holdings: dict) -> None: ...  # 每日调
    def notify_trade(self, trade: dict) -> None: ...
    def stop(self) -> dict: ...   # 返回 {metric_name: value}

class SharpeRatio(Analyzer):               # 组合:内嵌 TimeReturn 子 analyzer
    def __init__(self): self._tr = TimeReturn()
    def next(self, *a): self._tr.next(*a)
    def stop(self):
        rets = self._tr.stop()["time_return"]
        return {"sharpe": mean(rets)/std(rets)*sqrt(252) if rets else None}
```
- `run_backtest` 维护 `analyzers: list[Analyzer]`,每日 Phase 后调 `next`、结尾 `stop` 汇总进 metrics。
- 现有 `_metrics` 单函数拆成 Sharpe/Sortino/DrawDown/AnnualReturn/GrossLeverage 等;`TradeAnalyzer` 依赖 P2-8。
- **口径变更必须在 BACKTEST.md §8 记录**(如 sharpe 是否减无风险利率)。

### 9.4 P2-8 Position `opened/closed` 分解
```python
@dataclass
class Position:
    shares: int = 0
    avg_cost: float = 0.0
    non_closable: int = 0          # T+1 当日买入不可卖(rqalpha 对标,本轮可选)
    _lots: list = field(default_factory=list)   # FIFO 开仓批次 [(shares, price)]
    def apply_fill(self, delta_shares: int, price: float, fee: float) -> tuple[int, int, float]:
        """返回 (opened, closed, realized_pnl):区分本次成交的开仓/平仓部分。
        delta>0:全 opened,追加 _lots;delta<0:从 _lots 队首 FIFO 平(closed),算 realized。"""
```
- 开平配对:FIFO `_lots`,减仓平队首,realized = Σ(平仓份 × (price − lot_price)) − fee。
- `win_rate` 改为按"完整交易回合"(一个标的从建仓到清仓配对为一回合);`apply_action` 现金/权益不变,只多了配对统计。
- 回归:§7 基准的 cash/return/leverage 不变;**win_rate 口径会变**(属预期行为改变,在基准表标注或单独留旧口径对比)。

