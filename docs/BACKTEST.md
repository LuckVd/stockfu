# StockFu A 股回测系统：研究模式方案（2026-07-27 决策反转）

> ⚠️ **2026-07-27 重大决策反转**：本文已从「高可信 Raw + 自建多源仲裁账本」路线切换到
> 「**baostock 直用 + 前复权 + 允许误差**」研究模式。**新方案见 §0（当前唯一实施依据）**；
> 旧 §1–§13 降级为历史参考与技术附录。
>
> **文档状态：唯一权威（Single Source of Truth）**
> **生效日期：2026-07-27（决策反转版，取代同日早些时候的高可信版）**
> **适用范围：StockFu 的历史数据、因子研究、策略回测、模拟成交**
> 旧「高可信账本」路线（raw 账户 + 多源仲裁事件账本 + strict 准入门禁）**不再作为实施依据**；
> 其金标数学、A 股制度细节和问题清单现象仍保留作技术参考。

---

## 0. 研究模式方案（当前唯一实施依据）

### 0.1 定位

从「研究级高可信、可复现、无未来函数、自建账本」调整为「**研究方向正确、允许可控误差、尽快产出可用回测**」。硬性约束：

- **绝对不自建公司行为证据集/事件集**：放弃 `corporate_action_source_record` / `corporate_action_event` 的多源仲裁账本，直接用 baostock 分红接口落库的 `dividend_event`；
- **允许使用前复权（qfq）**做信号与收益计算；
- **允许红利税等用近似**（baostock 税后股利 `dividCashPsAfterTax`）；
- 目标是尽量精准的研究与回测，**不追求完全复现历史**。

第一项研究任务是**红利策略回测**；策略可交易范围沿用沪深300+中证500时点成分（已回补）。

### 0.2 为什么 baostock 够（三重验证）

经 DB 实测、baostock 官方文档、wrapper 源码交叉验证，原方案「必须自建账本」的前提**不成立**：

| 验证项 | 结论 | 证据 |
|---|---|---|
| baostock 分红接口字段 | **齐全**：除权日/派息日/红股上市日/税前+税后每股股利/送股/转增/登记日 | `dividInfo.md` 字段表；税后值已替你算红利税近似 |
| 早期（2000–2012）分红「空白」 | **回补不足，非源端缺失** | DB `dividend_event` 8632 行从 2000 年起，但早期每年仅 1–4 条；`get_dividend_metric(years=10)` 按需单查，从未全量回补 |
| pay_date/stock_mkt_date「缺失」 | **schema 没落库，非接口没返回** | `baostock_source.py:346-347` 已取，`dividend_event` 表无对应列 |
| K线 | 1990-12-19 起，三复权，「涨跌幅复权法」专为资金收益率设计 | `stockKData.md` |
| 估值 | peTTM/pbMRQ 日频（基于 raw close） | `valuationDaily.md` |
| 退市 | `outDate`/`status` 有退市日 | `stockBasic.md`（终值仍缺，用 exit_only 规避） |
| 财务 | 季报全套（profit/cashflow/balance/growth） | `season*.md` |

**baostock 源端真实局限**（研究模式下均可接受）：分红结果集有串行 bleed 脏数据（wrapper 已用 `abs(ex.year - y) > 1` 过滤）；复权静态、不可严格复现（冻结快照缓解）；退市终值缺（exit_only 规避）；红利税是固定扣税非持有期分档（用 AfterTax 近似）。

### 0.3 价格与公司行为口径（研究模式）

| 层 | 口径 | 说明 |
|---|---|---|
| 信号（股息率等绝对值逻辑） | **raw close + 税前分红** | 已实现于 `dividend_yield` 算子；禁止用 qfq 做股息率分母（前视） |
| 收益/净值 | **qfq 前复权**（涨跌幅复权法） | 资金收益率正确、已含分红再投；接受基准漂移 |
| 红利税 | **baostock `dividCashPsAfterTax` 近似** | 不自建持有期 FIFO 补扣账本 |
| 送转/拆股 | qfq 已调整 | 无需独立处理 |
| 退市 | `outDate` → `exit_only` | 退市日清仓，不追求终值结算 |
| 配股/合并/换股 | 暂不建模 | 受影响证券少，研究模式可忽略或剔除 |

与旧方案的根本区别：旧方案要求账户层 raw + 独立账本结算；新方案账户层接受 qfq Adjustment 语义（参考 Backtrader/聚宽/米筐主流做法），只在「绝对值逻辑」（股息率分母、PE/PB）上坚持 raw。

### 0.4 数据栈与实施步骤

数据栈：**baostock 单源**（不自建账本、暂不接 tushare）。行情走已稳定的 baostock raw + qfq；分红走 baostock `query_dividend_data` 灌 `dividend_event`。

实施顺序（最小成本验证优先）：

1. **实测 baostock 早期分红**：跑 `query_dividend_data` 查 2007–2012 某老股（如 `sh.600000`），确认源端数据真实存在与质量——唯一未坐实的未知；
2. **全量回补分红**：全市场 2007–2026 按 year 批量灌 `dividend_event`，填补早期空白；
3. **`dividend_event` 加列**：`pay_date` / `stock_mkt_date` / `per_share_cash_after_tax`，并改 wrapper 落库（DTO 已取，只差 schema + 写入）；
4. **简化回测引擎**：删除 strict Raw 账户路径与多源仲裁账本，主线切到 qfq Adjustment + 红利税近似；
5. **冻结一次 baostock 快照**作为回测基准，缓解复权基准漂移。

> **实施状态（2026-07-28）**：步骤 1/3/4 已完成；步骤 2 已遍历 2,023 只历史宇宙证券，分红 checkpoint 为 `success=2023`、`failed=0`。BaoStock 同日多笔分红的 13 个已审计冲突由显式裁决表按 `(证券, 除权日)` 合并，其中 `300315/2012-10-22` 合并为 0.15、`300760/2026-05-28` 合并为 1.56、`600989/2021-05-20` 合并为 0.58563；未知新冲突仍应拒绝静默写入。已执行 `--repair-known-dividend-conflicts` 与 `--backfill-dividend`，公司行为审计为 `duplicate_groups=[]`、`invalid_rows=[]`、`zero_event_years=[]`，`ready_for_formal_backtest=true`。经新浪不复权日线核验，`600472` 的 2007-12-18～20 三日真实成交及 12-21～26 停牌价已修复。研究主库 `data/stockfu.db` 是唯一数据源；`operator_result` 为可再生算子缓存，已物理迁至独立 `data/operator_cache.db`，回测不得写入行情、分红和历史宇宙表。
> 引擎 `valuation_basis` 已是 `raw`/`qfq`/`hfq` 三态、默认 `qfq`；strict 账户/账本代码（`engine` strict 分支、`VirtualAccount` 应收/行权方法、`corporate_actions.py`）及对应 CLI（`--strict`/`--stage-corporate-*`/`--materialize-corporate-actions`）已全部移除，交易约束（涨跌停/ST/list_date）解耦为 `universe_rules`/`execution_rules` 默认严格。
> 遗留：raw 诊断口径的 `credit_dividend` 仍按持有期 FIFO 扣红利税（§0.3 规划改用 afterTax 近似，未替换；qfq 主线 `credit_dividends=False` 不受影响）。

### 0.5 旧账本表处置

旧账本表 `corporate_action_source_record` / `corporate_action_event`（旧账本路线产物）已于 2026-07-28 **删除**：模型类移除 + `db.py:_migrate` DROP 回收空间（`init-db` 后实测 DROP，`dividend_event` 8632 行无损）。研究模式唯一公司行为来源是 `dividend_event`（baostock 直用），不再保留账本作诊断对照——多源仲裁能力随 strict 路径一并退役。

### 0.6 当前结果地位

研究模式下，回测结果定位为「研究方向可信、绝对数字允许误差」：

- 可用于策略趋势比较、因子方向判断、参数粗调；
- 不得宣称「税后精确年化 X%」（红利税为近似、复权有漂移）；
- 可复现性靠「冻结快照 + 固定 git commit」保证到研究可接受程度，不追求严格 PIT。

#### 0.6.1 标准绩效表（后续所有回测输出统一格式）

> **规范**：以后报告任一策略的回测绩效时，按下表分组与字段完整输出，不得自行增删列或换列名，便于跨策略、跨周期横向比较。`水下` 一律标注口径（默认=初始本金）。

以 `dividend_cross_section#sl30` 全周期（2021-01-04 → 2026-07-21，5.33 年 / 1343 交易日，初始 100 万，沪深300+中证500 时点成分日均 799 只，raw 口径，基准沪深300）为基准样例：

**配置头**

| 项 | 值 |
|---|---|
| strategy_id | dividend_cross_section#sl30 |
| 窗口 / 年数 / 交易日 | 2021-01-04 → 2026-07-21 / 5.33 年 / 1343 |
| 初始资金 | 1,000,000 |
| 票池(universe) | cn_historical_baostock_csi300_csi500_v1（日均 799 只） |
| 估值口径 | raw（不复权 + 现金分红入账） |
| 基准 | 沪深300 |

**收益指标**

| 指标 | 值 |
|---|---|
| total_return（总收益） | +44.1% |
| annualized（年化） | 7.1% |
| final_equity（期末权益） | 1,441,012.63 |
| excess（超额 vs 基准） | +54.13% |
| benchmark_return（基准收益） | -10.03% |

**风险指标**

| 指标 | 值 |
|---|---|
| max_drawdown（最大回撤） | 15.64% |
| max_drawdown_recovered（是否回本） | True |
| max_drawdown_recovery_days（回本天数） | 110 |
| sharpe（夏普） | 0.54 |
| sortino（索提诺） | 0.53 |
| calmar（卡玛） | 0.45 |

**水下指标（口径=初始本金；权益跌破初始本金 100 万的天数）**

| 指标 | 值 |
|---|---|
| 水下天数（权益 < 本金，underwater_pct_gt0） | 192 天（14.3%） |
| 水下 ≥10%（< 90 万）天数 | 0 天（0.0%） |
| 水下 ≥20%（< 80 万）天数 | 0 天（0.0%） |
| 水下 ≥30%（< 70 万）天数 | 0 天（0.0%） |

> 另：若需「相对历史最高点」的回撤口径，回撤 >10% 为 209 天（15.6%）、>20% 为 0 天；两种口径含义不同，引用时须注明。

**交易指标**

| 指标 | 值 |
|---|---|
| trade_count（总成交笔数） | 1,389 |
| **日均交易笔数** | **1.03 笔/天** |
| 年化交易笔数 | 261 笔/年 |
| win_rate（胜率） | 44.3% |
| distinct_stocks_bought（买入不同股票数） | 160 |
| **日均换手** | **0.1 只/天** |
| turnover_count（总换手只数，单边） | 133.0 |
| **年化换手（遍）** | **0.47** |

**止损指标**

| 指标 | 值 |
|---|---|
| stop_loss_count（止损触发次数） | 12 |
| stop_loss_realized_loss（止损实现亏损） | -143,864.90 元 |
| 单笔最大止损亏损 | -22,712 元（002572，2026-05-28） |

**分红指标（raw 口径；qfq 口径下分红折进价格，此组全为 0）**

| 指标 | 值 |
|---|---|
| cash_dividend_gross（现金分红税前） | 322,710.76 |
| dividend_tax_paid（红利税） | 25,857.59 |
| cash_dividend_net（分红净额） | 296,853.17 |
| cash_dividend_receivable（应收分红） | 0.00 |

**费用与仓位**

| 指标 | 值 |
|---|---|
| total_fee（总交易费用） | 15,374.82 |
| cash_constraint_hits（现金约束命中天数） | 34 |
| avg_gross_leverage（平均总仓位） | 93.5% |
| max_gross_leverage（最大总仓位） | 100.0% |
| max_single_weight（最大单仓权重） | 8.8% |

**撮合异常**

| 指标 | 值 |
|---|---|
| limit_reject_buys（涨停拒买） | 1 |
| limit_reject_sells（跌停拒卖） | 2 |
| fill_rejects（成交拒绝） | 3 |
| deferred_orders（挂单顺延） | 3 |

#### 0.6.2 候选策略验证结论（2026-08-01）

**方向背景**：原「平滑刹车」（`scale_all ×0.75`，`dividend_cross_section_partial_gentle_brake_take_profit`）只缩单票权重、组合每日重新填满，2008 危机下总敞口不降（回撤 69.52%）。本阶段在引擎层新增两个 opt-in 候选（旧路径逐字节不变）：

- **组合级敞口刹车**（`portfolio_brake_max_gross` / `portfolio_brake_tiers` 深度分级 + `portfolio_brake_recover_high_days` 滚动新高解除，策略 `dividend_cross_section_partial_exposure_brake_take_profit`）；
- **回撤加仓质量门控**（`portfolio_brake_scale >1` + `portfolio_brake_add_min_score` 仅对 strong_buy 加仓，策略 `dividend_cross_section_partial_drawdown_add_gated_take_profit`）。

**同窗口对照（2021-01-04 → 2026-07-21，5.33 年，raw，csi300+csi500 历史成分日均 799 只）**

| strategy_id | 收益% | 年化% | 回撤% | 夏普 | 胜率% | avg 敞口% | 止损 |
|---|---|---|---|---|---|---|---|
| partial_gentle_brake_take_profit（平滑刹车基线） | 80.00 | 11.66 | 15.00 | 0.83 | 59.9 | 95.8 | 14 |
| partial_exposure_brake_take_profit（base） | 70.87 | 10.58 | 16.83 | 0.76 | 79.7 | 94.4 | 16 |
| partial_exposure_brake_take_profit#deep | 63.92 | 9.72 | 15.05 | 0.71 | 78.7 | 95.5 | 19 |
| **partial_drawdown_add_gated_take_profit（scale1.2 门控）** | **101.12** | **14.01** | **14.24** | **0.96** | 82.4 | 96.5 | 12 |
| partial_drawdown_add_gated_take_profit#scale110（已弃） | 79.12 | 11.56 | 16.34 | 0.81 | 80.2 | 96.6 | 17 |
| partial_exposure_add_gated_take_profit（融合 tiers+门控） | 58.09 | 8.97 | 15.11 | 0.67 | 74.9 | 95.1 | 23 |

**长周期压力对照（2007-01-04 → 2026-07-21，19.5 年 / 4749 交易日，raw，含 2008 危机）**

| strategy_id | 收益% | 年化% | 回撤% | 夏普 | 胜率% | avg 敞口% | 止损 |
|---|---|---|---|---|---|---|---|
| partial_gentle_brake_take_profit（平滑刹车基线） | 319.76 | 7.91 | 69.52 | 0.43 | 65.2 | 97.6 | 129 |
| **partial_exposure_brake_take_profit（base）** | **421.78** | **9.16** | **54.09** | **0.60** | 65.0 | **72.6** | 119 |
| partial_drawdown_add_gated_take_profit（scale1.2 门控） | 260.77 | 7.05 | **71.50** | 0.40 | 68.0 | 98.3 | 134 |
| partial_exposure_add_gated_take_profit（融合 tiers+门控） | 436.95 | 9.33 | 56.47 | 0.57 | 66.7 | 78.9 | 121 |

**结论（定论）**

- **敞口刹车 base 是唯一长周期全面跑赢的方向**：2008 回撤从平滑刹车 69.52% 压到 **54.09%**，且收益（+421.78% vs +319.76%）、夏普（0.60 vs 0.43）均更高 → **定为候选**。机制：刹车期组合级总敞口真实下降（avg 72.6% vs 97.6%）。
- **回撤加仓门控只在近 5 年窗口成立**（+101.12% / 0.96），**长周期失效**：2008 危机下回撤 71.50%（三方向最差）、收益低于旧基线——危机中「加仓强者」放大风险，avg 敞口 98.3% 从不降 → 方向在长周期作罢（策略保留，可作近 5 年窗口参考）。
- **`#scale110`（1.10）≈ 无门控**（+79.12%），加仓火力不足；1.20 才是甜点位 → 变体已清理。
- **融合候选（tiers+门控，`partial_exposure_add_gated_take_profit`）未达预期**：长周期收益最高（+436.95%，超 base +421.78%）但回撤 56.47% / 夏普 0.57 均略逊 base；近 5 年全面落后（+58.09% / 0.67，三方向最差）。机制：近 5 年 tiers 0.95/0.80/0.65/0.50 频繁触发，总敞口被反复压降（avg 95.1%）拖累收益，而门控放大仅对未满单股上限目标生效、贡献不足；长周期 2008 保护成立（回撤 56.47% vs 门控 71.50%）但未优于纯 base → **方向作罢，策略保留（长周期收益优先备选）**。

**候选胜者标准绩效（敞口刹车 base，run-20260801-003431）**

配置：`dividend_cross_section_partial_exposure_brake_take_profit` base（tiers 0.85/0.75/0.60/0.45 + rec63，scale 1.0）| 2007-01-04 → 2026-07-21 / 19.5 年 / 4749 交易日 | 初始 1,000,000 | `cn_historical_baostock_csi300_csi500_v1`（日均 702 只）| raw | 基准沪深300。

| 指标 | 值 |
|---|---|
| total_return / annualized | +421.78% / 9.16% |
| final_equity | 5,217,767.81 |
| excess / benchmark_return | +292.51% / +129.27% |
| max_drawdown / 回本天数 | 54.09% / 1528 天 |
| sharpe / sortino / calmar | 0.60 / 0.55 / 0.17 |
| win_rate | 65.0% |
| trade_count / 止损笔数 | 3,597 / 119 |
| cash_dividend_net / total_fee | 1,473,788.25 / 116,668.70 |
| avg_gross_leverage / max | 72.6% / 100.0% |

#### 0.6.3 买卖不对称滞回 + 8 成仓验证（2026-08-01）

**方向背景**：持仓后分数小降即被 -dead 线清仓（对称死区）。本阶段新增**买卖不对称滞回**（opt-in，旧路径逐字节不变）：买入/持仓等权 1/1/1 算买入总分，卖出 2/1/2（低波降权、基本面升权）算卖出总分，各自归一化 ±100；持仓时卖出分跌破 -5 才清仓（买入线仍 +5），分数小降不追卖。另加组合 `max_gross=0.80`（8 成仓，留 2 成现金供高分票加仓）+ 冷却 30 交易日。策略 `..._exposure_brake_hold_take_profit`（=敞口刹车 base + 双总分）、`..._exposure_add_gated_hold_take_profit`（=融合 + 双总分）。

**同窗口对照（2021-01-04 → 2026-07-21，5.33 年，raw，日均 799 只）**

| strategy_id | 收益% | 年化% | 回撤% | 夏普 | 胜率% | avg 敞口% | 止损 |
|---|---|---|---|---|---|---|---|
| partial_exposure_brake_take_profit（base 对照） | 70.87 | 10.58 | 16.83 | 0.76 | 79.7 | 94.4 | 16 |
| **partial_exposure_brake_hold_take_profit（双总分）** | 59.23 | 9.12 | **11.70** | 0.76 | 80.1 | — | 13 |
| partial_exposure_add_gated_hold_take_profit（融合+双总分） | 56.03 | 8.71 | 12.65 | 0.72 | 79.5 | — | 14 |

**长周期压力对照（2007-01-04 → 2026-07-21，19.5 年 / 4749 交易日，raw，含 2008 危机）**

| strategy_id | 收益% | 年化% | 回撤% | 夏普 | 胜率% | 止损 |
|---|---|---|---|---|---|---|
| partial_exposure_brake_take_profit（base 对照） | 421.78 | 9.16 | 54.09 | 0.60 | 65.0 | 119 |
| **partial_exposure_brake_hold_take_profit（双总分）** | 380.97 | 8.69 | **50.83** | 0.59 | 66.0 | 106 |
| partial_exposure_add_gated_hold_take_profit（融合+双总分） | 350.63 | 8.32 | 53.70 | 0.54 | 67.2 | 134 |

**结论**

- **双总分滞回 + 8 成仓把回撤压到全系列最低**：长周期 50.83%（vs base 54.09%）、近 5 年 11.70%（vs base 16.83%），夏普与 base 持平（0.59 / 0.76）。
- **代价是收益同步下降**：长周期 +380.97%（vs base +421.78%）、近 5 年 +59.23%（vs base +70.87%）——`max_gross=0.80` 8 成仓永久放弃 20% 杠杆收益；冷却 30 交易日进一步降低交易（2373 vs 3597 笔）。回撤/收益同降、夏普持平 → 定位「低回撤稳健版」，非进攻增强。
- 双总分在融合变体上收益衰减更多（+350.63%），滞回加仓与门控叠加未产生正协同。
- 旧路径回归：敞口刹车 base 逐字节复现（70.87%/16.83%/0.76，run-20260801-133018）→ 改动完全向后兼容。

**运营注记（内存）**：2007 长窗回测曾因算子缓存全量预载（2007-2026 全区间 11.9M 行 ≈ 3.6G）超出宿主 3.7G 内存导致冻结重启；已改**滚动分块预载**（`CompiledStrategy.begin_run_cache` 只预载 250 日历日窗口、`prefetch_cache` 消费到尾部 20 日提前量内自动补块，`operator_cache.py` 零改动），峰值降到 ~1.5G 实测。长窗回测须 `BACKTEST_PROGRESS=1` 开进度日志并盯 RSS。

#### 0.6.4 train-test split 样本外筛查（2026-08-01）

**目的**：§0.6.2/§0.6.3 的候选均在 2007–2026**全样本**上调参挑选，有过拟合风险。本步将区间切开 train(2007–2016)/test(2017–2026) 各跑一遍**固定参数**回测，看样本外是否衰减。**仅筛查（固定参数切两段对比），非调参**——干净样本外须 walk-forward，见末尾。

**对照（raw，csi300+csi500 历史成分，沪深300基准；train 基准 +60.13% / test 基准 +41.8%）**

| 策略 | 夏普 train→test | 衰减比 | 超额 train→test | 样本外判定 |
|---|---|---|---|---|
| exposure_brake（base） | 0.67→0.42 | 0.63 | +143%→+16% | 中度衰减，仍正超额 |
| exposure_add_gated（融合） | 0.64→0.39 | 0.61 | +153%→+11% | 中度衰减，仍正超额 |
| exposure_brake_hold（稳健） | 0.61→0.32 | 0.52 | +105%→**−4%** | 衰减最重，**跑输基准** |

> drawdown_add_gated（对照）test 段因 background 回测被环境清理、两次补跑均未落盘而缺失；该策略全样本已证伪（回撤 71.50%），缺失不影响结论。

**结论（定论）**

- **三候选全样本调参均有过拟合衰减**（夏普 test/train 0.52–0.63）；超额主要来自 2007–2016，test 段（2017–2026）大幅缩水。
- **base/融合样本外维持正超额**（+16%/+11%），仍跑赢沪深300，可用——但**别指望 train 段那种 +140~150% 超额**（历史特定）。
- **hold 样本外跑输基准（−4%）**，三候选唯一转负：8 成仓 + 30 天冷却 + 滞回是**危机特化**的，在 2017–2026 温和上涨市（基准 +41.8%）踏空。**§0.6.3 的「低回撤稳健版」hold 方向样本外证伪，不再作为稳健候选推进。**
- ⚠️ **train/test 回撤不可比**（train 含 2008 危机、test 不含），回撤变小是市场环境差异非策略变好；判断过拟合用**夏普衰减 + 超额**，不用回撤。

**对认知的修正**：全样本（§0.6.3）看 hold 是「低回撤稳健版」、base 是「唯一全面跑赢」；切开后 hold 样本外反而最差——这正是切开验证的价值，也印证全样本调参挑选的候选必须做样本外筛查。

**下一步（未做）**：① walk-forward 调参（仅在 train 段网格搜索参数 → test 验证，才是干净样本外）；② 对齐中证红利/红利低波基准剥离风格 beta，看策略真实 alpha。**本环境 background 回测会被 harness 清理（非 OOM），后续多次回测须 `setsid`/`nohup` 脱离 + 手动轮询日志。**

#### 0.6.5 红利基准对齐 + alpha 存疑（2026-08-01）

**目的**：§0.6.4 用沪深300 判超额，但策略是红利低波风格，超额含风格 beta。本步对齐中证红利基准剥离风格 beta，看真实 alpha。（完成 §0.6.4 留的下一步②。）

**方法**：事后算（**不重跑**，避开 background kill）。基准只是事后参照，策略净值已存于 run；回补 `sh000922`（中证红利，baostock，2008-05-26 起 4412 行）后，用已有 equity_curve × 红利基准曲线算超额。train 段交集起点 2008-05-26（红利首日，000922 在 akshare/baostock 仅从 2008-05 有数据）。

**对照（raw 策略 vs 000922 价格指数 vs 沪深300，同交集起点）**

| 策略 | 段 | 策略收益 | 中证红利 | 超额 vs 红利 | 超额 vs 沪深 |
|---|---|---:|---:|---:|---:|
| base | train(08-05 起) | +105.9% | +9.0% | +96.9% | +112.9% |
| base | test | +58.0% | +32.1% | +25.9% | +16.2% |
| 融合 | test | +52.9% | +32.1% | +20.8% | +11.1% |
| hold | test | +37.4% | +32.1% | **+5.4%** | −4.3% |

**发现**：
- 对齐红利基准后三候选 test 段超额**全为正**（base +25.9% / 融合 +20.8% / hold +5.4%）；hold 从「跑输沪深 −4.3%」变「跑赢红利 +5.4%」——**§0.6.4「hold 证伪」部分是基准选择**（test 段沪深 +41.8% 跑赢红利 +32.1%，用沪深当基准对红利策略不公）。
- base/融合 vs 红利超额（+25.9%/+20.8%）**反而高于** vs 沪深（+16.2%/+11.1%）——test 段红利跑输沪深，对齐更弱基准策略显得更强。
- train 段策略高超额主要来自**选股 alpha**（风格 beta 仅占 13–19%）。

**⚠️ 重大 caveat：000922 是价格指数（不含分红），策略是 raw（含分红）→ 高估 alpha。** 中证红利年股息率 ~4.5%，test 段 9.5 年分红再投粗估增益 ~+60%，**全收益红利 test 段粗估 ~+90–100%**；策略 base test +58% → **可能跑输全收益红利 ~40%**。即对齐含分红的同类基准后，策略「alpha」可能蒸发甚至转负——高超额主要是**分红再投 + 红利风格暴露**，真实选股 alpha 存疑。

**结论**：「策略有无 alpha」**极度依赖基准选择**——沪深300 / 价格红利 / 全收益红利三套基准下，base test 超额从 +16% / +26% 到 ≈−40%。**当前结论：策略高超额 ≠ 真 alpha，很大程度是红利风格 + 分红再投；真实 alpha 待全收益红利指数验证后才能定论**（akshare/baostock 仅价格指数，全收益数据源 H00922/H30269 待确认）。

### 0.7 旧方案降级声明

本文 **§1–§13 为原「高可信 Raw + 自建账本」方案**，保留作历史参考与技术附录：

- **仍有参考价值**：金标数学（§5.7）、A 股制度细节（§5.3–5.5、§7）、问题清单现象（§3）；
- **不再生效**：必须 raw 账户、必须独立事件账本、必须多源仲裁、strict 准入门禁（§8）、分阶段实施 Phase 0–12（§10）中的账本/仲裁/strict 部分。

新实施依据以本 §0 为准；§1–§13 与 §0 冲突时，**以 §0 为准**。

---

## LEGACY-0. 原高可信方案：结论和不可变决策（⚠️ 2026-07-27 已推翻，仅作历史参考）

### 0.1 最终目标

建设一个可重复、无幸存者偏差、无未来数据泄漏、公司行为与中国 A 股制度可解释的
**天级事件驱动回测系统**。第一项正式研究任务是：

- 回测区间：`2007-01-15` 至数据快照的最后完整交易日；
- 数据保存范围：全部 A 股，包括仍上市、已退市、暂停上市和代码变更证券；
- 策略可交易范围：每个交易时点实际有效的沪深300与中证500成分股；
- 调入股票：达到成分生效时点后才允许买入；
- 调出股票：从生效时点起禁止新建仓和加仓，但可继续持有、减仓、止损和卖出；
- 账户口径：真实不复权价格成交和盯市，公司行为通过独立事件账本结算；
- 输出口径：至少同时提供税前总收益和个人投资者税后收益；
- 正式结果截止：当前数据库完整行情截至 `2026-07-24`，不得把未来公告事件混入。

`2007-01-01` 至 `2007-01-14` 只有沪深300，没有中证500。任何从元旦开始的结果必须明确
标注这段时间为“仅沪深300”，不得称作完整的300+500回测。

### 0.2 数据范围与策略范围必须分离

采用成熟量化系统常见的分层：

```text
全市场数据资产
    ↓ Dataset Snapshot（固定版本、截止日、来源、质量报告）
策略声明 Universe Provider
    ↓ 每日 Point-in-Time 可选集合 E(t)
因子与策略
    ↓ 目标仓位
引擎成交时再次执行 Universe/Tradeability 硬约束
    ↓ 账户、公司行为、税费、绩效
```

全市场数据的存在不意味着策略可以交易全市场。300+500限制由策略配置声明，但必须由
Universe Provider 和执行引擎强制执行，不能只在某个 YAML、算子或选股函数里过滤。
任何策略代码都不能绕过以下硬约束：

1. 信号日是否属于有效成分；
2. 订单执行日是否仍属于有效成分；
3. 调出后的买单和加仓单必须取消；
4. 调出后的卖单必须继续允许；
5. 停牌、涨跌停、退市等交易限制优先于策略意图。

### 0.3 价格与公司行为的唯一正式口径

| 层 | 正式口径 | 禁止事项 |
|---|---|---|
| 成交 | raw OHLC，不复权真实价格 | 禁止用 qfq/hfq 成交或计算股数 |
| 账户盯市 | raw close + 公司行为应收/已结算资产 | 禁止用 hfq 直接估值真实股数 |
| 技术信号 | 明确声明的 point-in-time 连续序列 | 禁止默认把任意 qfq 用于绝对价格逻辑 |
| 估值因子 | raw price、当时已发布财务数据 | 禁止名义现金/qfq、未来财报 |
| 公司行为 | 独立事件账本 | 禁止既调价格又调持仓造成双记 |
| hfq | 只用于数据对账和异常检测 | 正式账户模式禁用 |

正式方案等价于 LEAN 的 Raw 思路：原始价不修改，分红进入现金/应收，送转调整经济权益和
股份数量，退市、代码变化等作为事件进入引擎。参考：

- LEAN Corporate Actions：<https://www.quantconnect.com/docs/v2/writing-algorithms/securities/asset-classes/us-equity/corporate-actions>
- LEAN Data Normalization：<https://www.quantconnect.com/docs/v2/writing-algorithms/securities/asset-classes/us-equity/requesting-data>
- Zipline adjustment 数据结构：<https://github.com/quantopian/zipline>

### 0.4 当前结果的法律地位

在本文“正式准入门禁”全部通过前：

- 所有2007年开始的策略结果只属于探索性结果；
- 不得比较或宣传策略年化、夏普、最大回撤、超额收益；
- 不得根据探索结果删除、保留或实盘启用策略；
- 已有2021年以后结果也要在公司行为、费用和基准修正后重跑；
- 单元测试全绿只证明代码满足已有断言，不证明历史数据完整。

---

## 1. 成熟量化工具给出的正确流程

### 1.1 应借鉴的共同模式

LEAN、Zipline、RQAlpha 等成熟系统虽然实现不同，但可靠回测都具有以下边界：

1. **Security Master 独立存在**：上市、退市、证券标识、市场、板块和代码生命周期不是
   从今天的股票列表临时推断；
2. **历史宇宙是 point-in-time 数据**：今天的成分股不能倒灌过去；
3. **公司行为是第一等事件**：现金分红、送转、拆并、配股、换股、退市不能靠价格缺口猜测；
4. **Normalization Mode 明确**：同一资产只能选择一种账户结算语义；
5. **订单在执行时重新校验**：证券状态、宇宙成员资格、停牌、涨跌停会在信号后发生变化；
6. **Broker/Account 维护现金与持仓**：策略只产生意图，不直接修改现金和股份；
7. **结果携带完整数据谱系**：数据版本、代码版本、参数、费用模型和缺口必须随结果保存；
8. **缺数据应显式失败**：高可信模式不能用“回落到另一个字段”掩盖数据空洞。

### 1.2 本项目不照搬的部分

StockFu 当前是天级、A股多头研究系统，不需要一次复制成熟框架所有能力：

- 暂不实现分钟/tick撮合；
- 暂不实现融券、期权、期货和跨币种保证金；
- 暂不追求交易所逐笔队列成交；
- 日线下无法知道开盘后封板和排队细节，必须把简化写入结果；
- 免费数据源不能自动达到机构数据商质量，因此质量报告和拒绝机制比“自动修补”更重要。

### 1.3 对原调研报告的校正

原始调研保留在 `research/backtesting-design-research.md` 作为研究材料，但不是规范。使用时注意：

- Backtrader 默认 Broker 并没有调研报告所称的通用 `split()`/现金分红自动账本；
- “所有前复权都有未来函数”过于绝对。尺度不变信号可能不受常数缩放影响，但绝对价格、
  股数、成本和跨版本复现会受影响；
- IPO 条件应为 `list_date <= as_of`；
- 调整价格与调整持仓不能在同一模式重复执行；
- 报告没有覆盖支付日、送股上市日、卖出补扣红利税、零股和退市终值。

---

## 2. 当前系统实际上怎么运行

### 2.1 当前四层代码架构

```text
Operator
  stockfu/ai/operators/
    ↓ OpResult(score, confidence, signal...)
Compiled Strategy
  stockfu/ai/operators/runner.py + ai/strategies/*.yaml
    ↓ desired target weight
Rebalancer
  stockfu/ai/rebalancers/
    ↓ portfolio target weights
Execution Engine
  stockfu/backtest/engine.py
    ↓ VirtualAccount / trades / equity / metrics
Scheduler and Artifacts
  stockfu/backtest/scheduler.py
```

优点：

- 算子、策略、组合选择和执行已经分层；
- 所有历史取数设计上带 `as_of` 上界；
- T日收盘生成信号，T+1开盘执行；
- 算子缓存 fingerprint 包含源码 hash；
- 执行有停牌、涨跌停、滑点、整手和费用；
- 已实现调出持仓 `exit_only`；
- 回测产物含权益曲线、持仓、订单和指标。

### 2.2 当前每日时序

当前引擎大致按以下顺序运行：

```text
交易日 D：
  1. 读取 D 日行情
  2. 在除权日把现金分红直接加入可用现金
  3. 在除权日把送转直接加入可卖股份
  4. 用 D 日开盘执行 D-1 挂单
  5. 用 D 日收盘状态构建可选宇宙
  6. 计算因子、策略、rebalancer
  7. 形成 D+1 目标权重挂单
  8. 用收盘价记录净值
```

其中第2、3步只有经济权益时点大致正确，但“可用现金/可卖股份”时点不正确；第4步目前也没有
对订单执行日的指数成员资格做第二次硬校验。

### 2.3 当前数据库实测基线

截至2026-07-27审计：

| 项目 | 当前值 | 评价 |
|---|---:|---|
| `quote_snapshot` | 7,246,008行、2023只 | 300+500历史并集行情基本齐 |
| 行情日期 | 2006-01-04～2026-07-24 | 正式截止只能到07-24 |
| raw/hfq非空 | 现有行情行100% | 不代表应有交易日没有缺口 |
| 沪深300历史成员 | 939只 | 历史区间存在 |
| 中证500历史成员 | 1799只 | 历史区间存在 |
| 默认两指数历史并集 | 2023只 | 目标数据子集 |
| `security_master` | 801只 | 严重不足 |
| `delist_date` | 0只 | 退市语义不可用 |
| `dividend_event` | 8632行、781只 | 早期严重缺失 |
| 2007～2012分红事件 | 9行 | 不可接受 |
| 已入库送转事件 | 0行 | 代码已支持但数据未回灌 |
| 同代码同除权日重复 | 9组、18行 | 当前会双记 |
| 未来除权事件 | 34行 | 必须由快照截止日隔离 |
| 当前测试 | 118项通过 | 仅代码回归基线 |

历史成分快照存在两个明显问题：

- 大部分历史来源是 `baostock_*_snapshot_unverified`；
- 中证500早期有效成员只有497/498只，而不是500只。

BaoStock源端存在2007年以来raw K线、交易状态和公司行为字段，当前wrapper已将
`stocksPs + reserveToStockPs` 映射为送转比率。这说明2007起点没有被“源接口不存在”直接否决，
但不等于覆盖已验证：库内送转仍为0，早期现金事件仍严重缺失，退市也不得因样本数较少而省略。

### 2.4 当前存储容量

当前 SQLite：

- 物理文件约4.9GiB；
- 有效页约2.54GiB；
- 空闲页约2.4GiB；
- 行情表加三个索引约2.01GiB；
- 每行情行综合约298字节。

全A股2007年至今预计1700万～1900万行：

- 行情表加索引约4.7～5.3GiB；
- 整库有效占用约5.3～6.2GiB；
- 当前磁盘剩余约18GiB，能够保存全市场行情；
- 批量回灌、WAL、备份和VACUUM期间必须保留至少12～15GiB临时空间。

全市场行情可以保存，但禁止为全市场所有股票/日期/算子无差别生成缓存。当前254.9万行
`operator_result` 已约409MiB；全市场六算子全展开可能增长到12～18GiB。

---

## 3. 已发现问题清单

### 3.1 P0：会使结果失真的阻断问题

| ID | 问题 | 当前后果 | 正式要求 |
|---|---|---|---|
| P0-01 | 2007～2012公司行为几乎为空 | 分红少计、送转当暴跌 | 完成事件回灌和对账 |
| P0-02 | 9组重复分红 | 对持仓双倍派现 | 写侧唯一键、读侧防重、清理存量 |
| P0-03 | 无退市账本 | 持仓按末根价永久冻结 | 退市警告、取消订单、终止结算 |
| P0-04 | master仅801/2023 | 上市/退市/板块规则失效 | 全市场历史master |
| P0-05 | hfq仍可运行 | 整手失真；回灌送转后双记 | 正式模式直接拒绝hfq |
| P0-06 | 调出日未复核旧买单 | D-1买单可能在D调出后成交 | 执行时二次成员校验 |
| P0-07 | 红利税派息日扣 | 长持仓税负被高估 | 卖出时按最终持有期补扣 |
| P0-08 | 送转零股不能清仓 | 残余股份永久存在 | 卖出允许一次性清零股 |
| P0-09 | 除息触发成本止损 | 机械跌幅被当亏损 | 公司行为调整风险成本 |
| P0-10 | 基准为上证价格指数 | 与含分红策略不可比 | 300/500/800总收益基准 |
| P0-11 | open缺失时回落close成交 | 使用当日未来价格 | 无raw open必须顺延/取消 |
| P0-12 | 涨跌停与成交可得性无完整规则日程 | 不同板块/年份错判，封板虚假成交 | 按instrument×date计算限价并fail-fast |

### 3.2 P1：事件和资金可用性问题

1. strict路径现在只读取最新 `accepted` 的正式事件，并在除权日确认现金/股份应收；
   现金仅在 `payDate` 变为可用现金，送转仅在 `stockMktDate` 变为可卖股份；
2. 缺失支付日/上市日、最新revision不是`accepted`、或仍只存在旧`dividend_event`时，
   strict必须失败，绝不回退为除权日；
3. 该接入已覆盖现金、送转、混合distribution、带独立终值的退市结算，以及显式的配股行权/放弃；
   配股缺少accepted条款、上市日或显式策略，合并、代码变化，以及缺少独立终值的退市仍未建模，
   只要进入strict窗口便必须失败；
4. 现金红利的历史税率与卖出补扣尚未完成，当前strict账户只记录税前应收，不能据此宣称税后准确；
5. 混合事件只有一个粗粒度行，缺少稳定事件ID；
6. 配股、吸收合并、换股、要约收购、证券代码变化没有模型；
7. 公司行为发生在停牌期间时，`last_close` 可能仍是除权前价格，造成重复估值；
8. 未来公告事件虽然在表内，但数据快照没有统一 `known_at`/`cutoff` 隔离。

### 3.3 P1：其余执行与估值问题

1. 执行阶段用当日有开盘价的字典估算总权益，停牌持仓可能暂时按0参与仓位计算；
2. 未成交订单是目标权重而不是不可变订单对象，缺少创建时间、有效期和取消原因；
3. 没有成交量/参与率限制，大订单默认可在开盘一次成交；
4. 没有明确区分 settled shares、sellable shares、receivable shares；
5. 没有现金应收和受限现金；
6. 最后一个交易日生成但未执行的订单如何处理没有统一结果语义；
7. 日线无法观测封单队列和盘中排队位置，结果必须披露确定性保守假设及敏感性。

### 3.4 P1：历史制度问题

当前印花税只实现“2023-08-28前卖方千一、之后卖方万五”，遗漏：

| 区间 | 买方 | 卖方 |
|---|---:|---:|
| 2007-01-01～2007-05-29 | 1‰ | 1‰ |
| 2007-05-30～2008-04-23 | 3‰ | 3‰ |
| 2008-04-24～2008-09-18 | 1‰ | 1‰ |
| 2008-09-19～2023-08-27 | 0 | 1‰ |
| 2023-08-28以后 | 0 | 0.5‰ |

还需按日期和交易所核实：

- 过户费历史分段；
- 佣金最低收费和用户可配置佣金；
- 2013年前红利税；
- 2013年至2015-09-07的5%/10%/20%差别税负；
- 2015-09-08后的0%/10%/20%及卖出补扣时点。

正式实现必须引用财政部、税务总局、证监会和中国结算原始政策，并把政策表作为版本化数据，
不能继续散落在代码常量中。

### 3.5 P2：研究方法问题

1. qfq只对尺度不变算子近似安全，尚无算子价格口径白名单；
2. 财务因子需要披露日而不是报告期；
3. 估值缺失、停牌、ST和上市不足天数的处理需要年度报告；
4. 因子诊断可能在全市场和策略宇宙间混用截面；
5. benchmark是价格收益，策略是含分红税后收益；
6. 当前回测结果没有完整数据快照ID、事件版本和Git commit；
7. 对源端错误仍有过多自动fallback，严格回测应fail-fast。

---

## 4. 目标数据架构

### 4.1 数据域

```text
Reference Domain
  security_master
  security_identifier_history
  trading_calendar
  exchange_rule_schedule

Market Domain
  quote_snapshot / market_bar_daily
  adjustment_factor_daily

Corporate Action Domain
  corporate_action_event
  corporate_action_source_record

Universe Domain
  index_constituent
  universe_definition

Research Governance Domain
  dataset_snapshot
  data_quality_report
  backtest_run_manifest
```

现阶段可继续使用单SQLite，不需要为了全市场立即迁移数据库。冷历史未来可以按年份导出Parquet，
SQLite继续保存索引、主数据、事件、热数据和manifest。

### 4.2 `security_master`

范围必须是所有曾上市A股，而不是当前股票或历史指数并集。至少包含：

```text
security_id              永久内部ID，不复用股票代码
code                     当前或该生命周期代码
exchange                 SSE/SZSE/BSE
board                    main/chinext/star/beijing
name
list_date
delist_date
status
security_type
currency
source
source_version
revision
known_at
observed_at
ingested_at
```

代码变化另存 `security_identifier_history`：

```text
security_id | code | valid_from | valid_to | reason | source
```

账户、公司行为和指数成分最终应关联 `security_id`，展示和数据源查询才使用代码。

#### 交易日历

- 按交易所和日期物化 `trading_calendar`，不在每次运行时临时由周末/节假日推断；
- `exchange_calendars.XSHG`可作为生成种子，但必须与交易所公告及至少一个独立交易日源交叉核验；
- 生成器版本、时区、早收市/临时休市例外和逐日内容hash进入snapshot；
- 一旦进入snapshot，库升级或日历修订不得改变旧回测的交易日轴。

### 4.3 日行情

正式必需字段：

```text
security_id / asset_code
trade_date
raw OHLC
volume
amount
trade_status
is_st
pre_close
limit_up_price
limit_down_price
source
source_version
revision
known_at
ingested_at
```

规则：

- raw为唯一成交和盯市字段；
- qfq/hfq保留作研究或对账，但必须有清楚的供应商和生成版本；
- `pre_close` 应直接保存，避免用当日收盘和涨跌幅反推；
- volume需要在连续信号序列中按拆股因子相应调整；
- 逻辑键为 `(security_id, trade_date, source)`，修订只能插入新 `revision`，不允许覆盖旧版；
- 严格模式不允许raw缺失时回落qfq；
- 停牌是明确状态，不等于“数据缺失”。
- 获取层允许在明确优先级下fallback，但每行必须保存实际source/provenance，且不得把不同复权/字段口径相互补位；
- 回测运行层不允许网络取数或数据源fallback；只读snapshot中缺失关键输入时抛出类型化错误并fail-fast。

### 4.4 公司行为

统一表至少包含：

```text
action_id                稳定事件ID
security_id
action_type              cash_dividend / stock_dividend / split / rights /
                         merger / symbol_change / delisting
announce_date
record_date
ex_date
pay_date
stock_listing_date
per_share_cash_before_tax
per_share_stock
rights_ratio
rights_price
exchange_ratio
currency
status                   proposed / approved / implemented / cancelled
source
source_ref
source_version
revision
supersedes_action_id
known_at
observed_at
ingested_at
resolution_status        accepted / merged / rejected / ambiguous
```

原始供应商每一行先进入 `corporate_action_source_record`，规范化和仲裁后才能进入正式事件表。
禁止直接把有冲突的供应商行写成可结算事件。
源记录和规范事件都采用append-only版本；修正通过新revision和取代关系表达，不对已被snapshot引用的行做UPDATE/DELETE。

当前实现的表名为 `corporate_action_source_record` 与 `corporate_action_event`：前者按
`(source, source_event_key)` 幂等保存来源观测及payload hash，后者以
`(action_id, revision)` append-only 保存仲裁结果。条款一致的多源记录可提出
`accepted`；单一来源、现金/送转/配股等经济条款冲突，或记录日、支付日、送转上市日等
结算日期互相矛盾时必须为 `needs_review`，不能按抓取顺序覆盖。一个来源缺失而另一个来源
提供的日期可补全，不构成冲突。预案公告日与实施公告日的语义差异保留为warning，不阻断
已对齐的经济条款和结算日期。
同一供应商的不同财年、接口或抓取批次不是独立来源，不能据此自动接受。
仅为展示股息率而按除权后股本摊薄的遗留记录保留作证据，但不能否决同供应商的
“除权前旧股”账户事件来源；仲裁按每个提供方的最高证据等级取值。

截至2026-07-27的实现验证：

- 主库已在一致性备份后创建上述两张表；BaoStock 返回的 `payDate` 与 `stockMktDate` 已沿 DTO 暂存至来源证据表；strict回测现只读取每个逻辑事件最新的`accepted` revision，旧`dividend_event`仍未被改写且只能用于探索路径；
- strict账户在除权日只确认现金/股份应收，分别到支付日和新增股份上市日才解锁；缺少这些日期、最新revision不是`accepted`、或出现配股/合并等未建模类型、以及无终值退市时都会在运行前阻断。历史红利税的卖出补扣仍未实现，因此尚不能宣称税后结果准确；
- strict模式已拒绝`valuation_basis=hfq`，以避免供应商复权价与公司行为账本双记；`hfq`只保留给`strict=False`的探索性诊断；
- 退市账本已增加`terminal_price`审计字段与账户终止结算：仅当最新accepted事件给出独立核验的正终值时，才以该终值关闭持仓；warning会取消遗留买入/加仓订单。当前交易所名单没有终值，故仍全部阻断strict；
- 配股账户支持显式`exercise_if_cash_available`与`ignore`：前者按可用现金认购整数股、冻结认购款并在上市日以认购成本生成新lot；后者不伪造权利价值。默认`reject`，缺少明确策略、比例/价格、上市日或accepted条款时strict失败。`600030` 2022-01-27 已由巨潮实施数据和发行人A股配股结果公告双源接受，可作为配股账户金标；
- `000629`：2009、2011、2014纯现金事件已双源接受；2007现金/送转条款冲突、2012单源送转保持 `needs_review`；
- `300024`：BaoStock 与按除权前旧股解析的 AkShare 在2010、2011、2014混合现金+送转事件上对齐；2016仍有条款冲突；
- `300024` 2010-04-16 的 `10派1、10送2、10转10` 已有账户金标：每旧股现金`0.1`、新增股份`1.2`，100股在除权后为220股；用raw除权价`4.5`盯市并计入10元现金时权益保持1000元（假设持有超过一年、无红利税）。
- `600030`：2022-01-27配股为每旧股可认购`0.15`股、配股价`14.43`元、记录日`2022-01-18`、上市日`2022-02-15`。巨潮实施数据与[发行人A股配股发行结果公告](https://www.citics.com/newsite/tzzgx/ggyth/gg/aggg/202201/P020220328648174712581.pdf)条款一致，已物化为revision 2的`accepted`事件；发行人公告确认每10股配1.5股，交易所上市公告亦确认新增股份于2月15日上市。该结论仅覆盖此单事件，不代表其他配股已覆盖。
- 交易所退市名单已接入：深交所208条“终止上市”暂存为`delisting`，上交所153条仅披露“暂停上市日期”的记录暂存为`delisting_warning`。全部361条因缺少独立终止结算价/后续现金分配而保持`needs_review`；为外键完整性仅补充最小`asset`身份，未将名单日期写入`security_master.delist_date`。

### 4.5 历史指数宇宙

继续使用右开区间：

```text
effective_from <= trade_time < effective_to
```

每行需保留：

```text
index_code
security_id
announce_at
observed_at
known_at
effective_from
effective_to
source
source_ref
source_version
revision
ingested_at
verified
```

必须区分三个时间：

- `announce_at`：官方公告对市场公开的时间，不得只存日期而丢失开/收盘边界；
- `observed_at`：本系统或供应商首次实际观察到该记录的时间；
- `known_at`：策略运行时有权使用该成分信息的时间；
- `effective_from`：何时正式成为/不再是成分。

绑定裁决规则：

1. 有可核验官方公告时，`known_at = announce_at`，但成分资格仍只在 `effective_from`
   到达后生效；公告日买入尚未生效的调入股不属于指数成分策略。
2. 只有BaoStock按日快照且无历史公告时，`known_at`取首次可证明的 `observed_at`；
   若在收盘后观察，最早下一交易日可用。
3. 禁止统一写成“`known_at = effective_from` 的下一交易日”；这会对已提前公告的
   正常调样人为延迟一日。
4. 信号日可选集同时满足 `known_at <= decision_time` 与
   `effective_from <= decision_time < effective_to`；订单在执行日再次满足
   `effective_from <= execution_time < effective_to`。

### 4.6 数据快照和可复现性

每次正式回测只能引用冻结的 `dataset_snapshot_id`。snapshot是“行选择规则+内容哈希”，
不是在可变表上贴一个 `immutable=true` 标签：

```json
{
  "dataset_snapshot_id": "a_share_20070115_20260724_v1",
  "market_data_through": "2026-07-24",
  "ingested_at_cutoff": "2026-07-27T12:00:00+08:00",
  "security_master_version": "...",
  "quote_version": "...",
  "corporate_action_version": "...",
  "universe_version": "...",
  "fee_tax_schedule_version": "...",
  "trading_calendar_hash": "...",
  "component_hashes": {"quotes": "...", "actions": "...", "universe": "..."},
  "component_row_counts": {"quotes": 0, "actions": 0, "universe": 0},
  "quality_report_hash": "...",
  "created_at": "...",
  "immutable": true
}
```

单SQLite落地规则：

1. raw source-record、规范事件、行情和成分都不允许删改旧版；行情更正以新revision追加。
2. snapshot在一个一致性读事务内，固定 `ingested_at <= ingested_at_cutoff` 并为每个逻辑键选定
   当时最新accepted revision；日后倒灌的旧日期数据也不得进入旧snapshot。
3. 对排序、序列化规范固定后的实际选中行计算SHA-256，同时保存行数、范围和选择规则。
4. `dataset_snapshot`、manifest和被引用revision必须由数据库约束/触发器禁止UPDATE/DELETE；
   只靠应用约定不能声称不可变。
5. 正式运行用只读连接，算子缓存和回测结果写入独立 `runtime/cache.sqlite` 或文件目录，
   不向snapshot所在数据库写入。
6. 每次运行前重算组件hash；任一hash或行数不符立即失败。重要发布snapshot可再导出SQLite backup作物理归档，
   但不以每次全库拷贝作为唯一不可变手段。

同一snapshot、同一Git commit、同一配置必须生成相同结果hash。

---

## 5. 目标回测引擎

### 5.1 每日事件时序

目标天级时序：

```text
D日开盘前
  1. 推送截至D开盘前已知的公告/状态事件
  2. 处理D日除权：
       - 生成现金应收
       - 生成股份应收
       - 调整经济权益成本基准
       - 不提前增加可用现金/可卖股份
  3. 处理D日支付：
       - cash_receivable → available_cash
  4. 处理D日新增股份上市：
       - receivable_shares → settled/sellable_shares
  5. 处理代码变更、退市警告、退市执行
  6. 重新校验全部未成交订单：
       - 成员资格
       - 上市/退市状态
       - ST/停牌/涨跌停规则
       - 有效期

D日开盘
  7. 只用raw open尝试成交
  8. 先卖后买
  9. 费用按D日政策计算
 10. 不存在raw open则顺延/取消，绝不使用D日close

D日收盘
 11. 用raw close和应收资产盯市
 12. 构建D日信息集
 13. 构建D或D+1可交易宇宙（取决于公告是否已知）
 14. 计算point-in-time信号
 15. 策略和rebalancer生成目标仓位
 16. 风控处理
 17. 生成D+1订单
 18. 保存账户、事件、订单、质量告警
```

### 5.2 账户状态

`Position` 目标字段：

```text
settled_shares            已登记股份
sellable_shares           当日可卖股份
receivable_shares         已享有但未上市股份
avg_accounting_cost       会计成本
risk_reference_cost       公司行为调整后的风控成本
lots[]                    FIFO买入批次
dividend_entitlements[]   每批股份享有的分红及递延税
```

`lots[]`采用队列结构，每批至少保存 `acquired_at / settled_at / quantity /
sellable_quantity / source_order_id / source_event_id`。同一队列同时支撑T+1可卖数、FIFO持有期红利税、
送转批次缩放和换股successor迁移；不再为四类问题各维护一套股份计数。

现金目标字段：

```text
available_cash
cash_receivables[]
restricted_cash
tax_liabilities[]
```

权益：

```text
equity =
  available_cash
  + cash_receivables的可收回价值
  + settled_shares × raw mark
  + receivable_shares × 合理raw mark
  - tax_liabilities（税后模式）
```

### 5.3 现金分红和红利税

除息日：

- 按登记日持仓生成分红权利；
- 税前模式记全额应收；
- 税后模式生成递延税责任，不能按当时持有期最终结算；
- raw价格除息不应导致权益机械跳变；
- 应收现金不能用于买入。

支付日：

- 应收转为可用现金；
- 若支付日缺失，采用明确且写入结果的保守规则，不能默认为除息日。

卖出日：

- FIFO确定卖出批次；
- 对该批次历史分红按最终持有期和当日有效政策补扣；
- 部分卖出只结算对应股份；
- 超过一年后卖出按适用历史政策减免。

三个时点不得混用：`ex_date`确认权益，`pay_date`使现金可用，卖出日按最终持有期结算递延税。
Zipline亦分开保存ex/pay日，并在ex日登记unpaid dividend、pay日转现金；本项目只借鉴这个时序边界，
税务仍以中国官方政策表为准。参考：
<https://github.com/stefan-jansen/zipline-reloaded/blob/main/src/zipline/data/adjustments.py>、
<https://github.com/stefan-jansen/zipline-reloaded/blob/main/src/zipline/finance/ledger.py>。

### 5.4 送转、拆股和零股

除权日：

- 生成应收股份；
- 经济股份数用于权益连续性；
- 原股份仍按实际可卖状态管理；
- 均价和风控参考成本按因子调整。

上市日：

- 应收股份转为可卖股份；
- 保留原lot购买日期用于红利税；
- 送转本身不重置持有期。

卖出：

- 买入仍按交易制度要求；
- 减仓默认按整手；
- 目标为0时允许一次性卖出全部零股；
- 反向拆股/合并产生的小数股份按官方现金替代规则处理。

### 5.5 配股、合并和退市

配股必须配置策略：

- `exercise`：需要现金、增加股份；
- `sell_right`：若权利可交易且有行情；
- `ignore`：视为放弃，但仍承担除权价值影响。

默认高可信个人账户模式建议 `exercise_if_cash_available`，不足现金时记录部分/放弃；在没有完整
配股数据前，受影响证券不得静默进入正式结果。

退市：

1. warning事件：允许策略主动卖出；
2. 取消所有买单和加仓单；
3. delisted事件：按官方退市结算价、最后可交易价或后续现金分配规则处理；
4. 无可靠终值时不能简单沿用末根价格，也不能默认归零；
5. 无法解释的退市进入reject清单，并使严格回测失败。

吸收合并/换股：

- 原证券持仓关闭；
- 按换股比例生成新证券应收股份；
- 现金选择权单独结算；
- 新证券上市后可卖。

### 5.6 订单模型

订单至少保存：

```text
order_id
created_at
signal_date
intended_execution_date
security_id
side
target_weight / quantity
order_type
time_in_force
universe_id
membership_at_signal
membership_at_execution
status
cancel_reason
source_signal
```

执行日重新校验：

- 调出股票：取消buy/add，保留sell/reduce；
- 尚未调入：取消buy；
- 停牌/跌停卖不出：按配置顺延；
- 退市执行：取消普通订单，转退市结算；
- 缺开盘价：顺延/拒绝，禁止close fallback。

#### 日级成交可得性的绑定模型

正式回测的基准订单是“T日收盘生成、T+1开盘尝试”。日线无法观察封单队列，因此采用可重现的保守模型：

1. `trade_status != trading`、raw open缺失/非法、或执行日限价规则无法确定：不成交；
   最后一种在strict模式下不是普通顺延，而是数据门禁失败。
2. 优先使用来源可追溯的 `limit_up_price / limit_down_price`；否则用前收、版本化规则日程、
   最小价格变动单位和交易所舍入规则计算，禁止仅用 `pct_chg`容差猜测。
3. 买单raw open已在涨停价，或卖单raw open已在跌停价：基准模型不成交，按time-in-force顺延/取消；
   不因当日后来打开过就假设开盘订单排队成功。
4. `open = high = low = close = 不利方向限价`的一字板必须不成交；volume只作交叉证据，
   不能因当日有少量成交就假设策略订单获得成交。
5. 开盘不在不利限价且其他条件合法时，才可按raw open+配置滑点成交；成交量参与率是额外的容量上限。
6. 结果必须分开报告停牌、无open、一字板、开盘封板、计算限价缺失和参与率限制的拒绝/顺延数。

`exchange_rule_schedule`必须至少以 `exchange / board / instrument_status / effective_from /
effective_to`为维度保存涨跌停比例、tick size、舍入和无限价例外。不能把“ST=5%、主板=10%”写成全历史常量：

- 科创板上市前5个交易日无涨跌停，之后20%：
  <https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20190719_4866745.shtml>；
- 创业板注册制首批上市起存量同步改为20%，且风险警示股存在制度边界：
  <https://www.szse.cn/www/investor/index/update/t20200729_580056.html>、
  <https://www.szse.cn/disclosure/notice/general/t20200710_579459.html>；
- 北交所常规30%、上市首日无涨跌停：
  <https://www.bse.cn/important_news/200010675.html>；
- 上交所主板风险警示股在2026-07-06由5%改为10%：
  <https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20260424_10816474.shtml>。

上述只是必测的代表性边界，不是完整历史日程；正式库仍须逐条引用生效当时的交易所规则。

### 5.7 信号价格口径

`split_adjusted_pit` 和 `total_return_adjusted_pit` 都是运行时视图，不是供应商复权列，
也不是预先把整段历史覆盖成的物化价格。正式账户始终使用raw+事件账本；视图只向信号算子提供连续窗口。

对于计算前沿 `t = as_of`、历史bar日 `d <= t`，定义：

```text
eligible_events(d, t) = {
  e | d < e.ex_date <= t
      and e.known_at <= t
      and e.status = implemented
      and e.resolution_status = accepted
}

A_price(d | t) = ∏ price_ratio(e), e in eligible_events(d, t)
P_adj_ohlc(d | t) = P_raw_ohlc(d) × A_price(d | t)
```

每个因子只能使用它自己的历史 `as_of`，不得用整次回测的end date作计算前沿。因此日后延长回测截止日时，
历史时点t的视图和信号不得变化。

单一事件比率的最低规范（全部按每旧股口径）：

```text
P_prev = ex_date前一有效交易日的raw close
D      = 每股税前现金分红
m      = 1 + 送股率 + 转增率（拆/并股使用对应股份乘数）
r      = 每旧股可认购的配股数
K      = 配股价

theoretical_ex_price = (P_prev - D + r × K) / (m + r)
price_ratio          = theoretical_ex_price / P_prev
```

金标数学例（均忽略当日自然涨跌）：

| 事件 | 输入 | `price_ratio` | 连续性预期 |
|---|---|---:|---|
| 每股派0.5元 | `P_prev=10,D=0.5,m=1` | 0.95 | 事件前10元调为9.5元 |
| 10送3 | `P_prev=10,D=0,m=1.3` | 0.769230769230… | 事件前10元调为7.692307…元 |
| 10送3并派5元 | `P_prev=10,D=0.5,m=1.3` | 0.730769230769… | 理论除权价7.307692…元 |
| 10配3，配股价8元 | `P_prev=10,D=0,m=1,r=0.3,K=8` | 0.953846153846… | 理论除权价9.538461…元 |

- `split_adjusted_pit`只累乘送转/拆并的price ratio，不包含D和配股价值；
- `total_return_adjusted_pit`累乘所有已支持、已accepted的price ratio，现金分红使用税前D；
- 同一ex_date的现金、送转和配股先仲裁为一个组合经济事件，只计算一个组合ratio；禁止把同日各分量独立累乘。
- 只有股份乘数影响量：对事件前volume按股份比例反向调整；现金分红不调volume；
- 配股只有在事件账本已完整建模并声明行权假设时才可使用上式；否则该窗口strict失败；
- `P_prev <= 0`、ratio非正、同日事件未仲裁、或任一机械除权台阶无法解释时，禁止生成视图。
- 金额和ratio使用十进制高精度（至少18位有效数字）累乘，中间因子不按行情小数位舍入；仅在展示或交易所限价计算时按对应规则量化。

Zipline对现金分红使用 `1 - D / P_prev` 生成作用于过往价格的ratio，且价格乘ratio、拆股时volume除ratio；
上述定义将这一机制扩展为A股送转/配股公式。参考：
<https://github.com/stefan-jansen/zipline-reloaded/blob/main/src/zipline/data/adjustments.py>。

与供应商复权的关系写死为：

> `total_return_adjusted_pit` 只能由冻结raw bar+冻结accepted公司行为在本地构造；
> 禁止读取、复制、播种或回落到供应商qfq/hfq及其adjustment factor。供应商hfq是独立诊断对照，
> 不是本视图的数据源，也不能满足本视图的覆盖门禁。

每个算子必须声明：

```text
price_normalization:
  raw | split_adjusted_pit | total_return_adjusted_pit
scale_invariant:
  true | false
volume_adjustment:
  raw | split_adjusted
```

建议：

- 动量、趋势、布林、波动率：`total_return_adjusted_pit`；
- 成交、涨跌停、成本、股息率分母：raw；
- 成交量类指标：价格和量分别按拆股因子处理；
- PE/PB：使用当时raw价格和point-in-time财务数据。

短期可保留现有qfq供尺度不变算子，但必须逐算子白名单，并用不同回测截止日重灌数据后验证信号
完全一致。上述运行时视图借鉴LEAN ScaledRaw的“按当前时点调整历史窗口”边界，但具体比率以本节公式为唯一规范。

---

## 6. 策略宇宙：全市场存储，300+500交易

### 6.1 集合定义

```text
A              全市场数据集合
E(t)           t时点可新建仓/加仓集合：有效300+500成分并通过上市/ST/交易过滤
H(t)           t时点实际持仓
M(t)           持仓管理集合：E(t) ∪ H(t)
X(t)           exit-only集合：H(t) - E(t)
```

规则：

- 因子截面排名只能在E(t)内计算；
- X(t)不能占用新候选名额；
- X(t)的目标仓位不能高于当前仓位；
- X(t)仍必须计算卖出、止损和退市处理；
- 引擎在订单生成和订单执行两个阶段都执行上述规则；
- 全市场A只用于存储、数据质量、未来策略和必要的事件关联。

### 6.2 策略配置

目标配置示例：

```yaml
strategy_id: dividend_cross_section
data_scope: all_a_shares
universe:
  provider: point_in_time_index_union
  indices: [000300, 000905]
  membership_mode: verified_effective_interval
  on_removed: exit_only
  on_added: eligible_from_effective_time
  min_list_days: 60
  exclude_st_for_entry: true
execution:
  frequency: daily
  signal_at: close
  execute_at: next_open
  strict: true
```

`data_scope` 不是 `universe`。策略配置错误、宇宙版本缺失或成分覆盖不完整时必须拒绝运行。

### 6.3 性能边界

保存全市场不等于每次回测把全市场全部预载：

- 300+500策略只解析回测窗口内的历史成员并集；
- 预载候选并集、持仓和必要基准；
- 算子缓存只为E(t)和M(t)计算；
- 全市场策略以后需要分块/列式/Parquet扫描，不能一次ORM物化；
- `operator_result` 设置按snapshot和策略宇宙的清理/保留策略。

---

## 7. 费用、税务和基准

### 7.1 费用政策表

把代码常量改为版本化 `exchange_rule_schedule`：

```text
effective_from
effective_to
market
side
fee_type
rate
minimum
source_ref
version
```

必须覆盖：

- 印花税全部历史分段；
- 沪深过户费历史；
- 佣金可配置；
- 最低佣金；
- 可能的经手费/监管费是否包含在佣金假设中；
- 结果中披露“实际建模费用”和“未建模费用”。

### 7.2 两种账户报告

正式运行同时输出：

1. `gross_total_return`：公司行为税前、扣交易费用；
2. `individual_net_return`：按个人投资者历史红利税、扣交易费用。

以后可增加机构、基金等税务profile，禁止用一个硬编码红利税代表所有投资者。

### 7.3 基准

最低基准集：

- 沪深300价格收益；
- 沪深300总收益；
- 中证500价格收益；
- 中证500总收益；
- 300+500等权或按指数市值组合的可复现总收益基准；
- 上证综指仅作市场环境参考。

策略含分红时，主要超额必须对总收益基准。基准本身也需要snapshot、公司行为和数据源版本。

---

## 8. 数据质量和正式准入门禁

正式 `validated`门禁默认是二元判定，不使用一个笼统的“99%覆盖率”代替：

- 关键完整性分母是回测日历上 `E(t) ∪ H(t)` 所有应有的security-day和生效事件；
- 关键数据覆盖必须100%，未解释缺失、冲突、unsupported事件和unknown成交输入必须为0；
- 供应商源记录可以被rejected，但同一经济事件必须有唯一accepted规范结论；
- 不得通过静默剔除当时指数成分来凑覆盖率；任一受影响的 `E(t)`/`H(t)` 记录使整次strict运行FAIL。
- 可另行产生明确改变宇宙的exploratory剔除实验，但不得与完整300+500 validated结果同级。

### 8.1 主数据门禁

- 回测数据范围内所有证券都有永久ID；
- `list_date`覆盖100%；
- 已退市证券的`delist_date`和终止结算状态覆盖100%；
- 代码区间无重叠、无复用歧义；
- 板块和交易所可在每个历史日期确定；
- master缺失一只即严格回测失败。

### 8.2 成分门禁

- 沪深300每个有效快照300只；
- 中证500每个有效快照500只；
- 起始日至结束日无时间缺口；
- 调入调出区间不重叠；
- 无指数代码污染；
- 官方公告/官方样本优先；
- 未验证快照不得进入最高可信等级；
- 497/498只的历史快照必须修复；未修复日期使完整300+500 strict运行FAIL。

### 8.3 行情门禁

- `(security_id, trade_date)`无重复；
- 成分有效期和持仓期内每个交易日必须能区分交易、停牌和数据缺失；
- raw OHLC非空且价格关系合法；
- pre_close、涨跌幅和OHLC可交叉校验；
- raw/qfq/hfq供应商版本明确；
- 异常涨跌、零价格、负成交量进入报告；
- 数据缺失不能静默字段回落。

### 8.4 公司行为门禁

- 同一事件只有一个accepted规范事件；
- 9组存量重复清零；
- 2006年以来现金、送转回灌完成；
- 事件日期、金额、比例和状态可追溯到原始行；
- 混合事件能同时结算现金和股份；
- 每个复权因子台阶都能由已知事件解释，或进入人工拒绝报告；
- `E(t) ∪ H(t)`中出现模型不支持的配股/合并/退市时，完整strict运行FAIL；
- 除权日权益连续性的合成测试全部通过。

### 8.5 成交可得性门禁

- 每个订单执行日都能确定交易所、板块、证券状态、ST状态、tick size和适用的限价规则，覆盖100%；
- `limit_up_price / limit_down_price`来自可追溯源或可由版本化schedule逐分重算；
- 任意filled订单必须存在合法raw open，无open、停牌、一字板和不利开盘封板不得成交；
- 无法确定限价的订单不得按“可成交”回落，strict运行直接FAIL；
- 每类拒绝/顺延都有reason code、数量、名义金额和对结果的敏感性报告；
- 所有历史规则切换日前后均有金标测试。

### 8.6 执行不变量

每天断言：

```text
available_cash >= -tolerance
settled_shares >= 0
sellable_shares >= 0
receivable_shares >= 0
sum(lot shares) == entitled shares（按模型定义）
sellable_shares <= settled_shares
调出证券无buy/add成交
未上市/已退市证券无普通买入
成交价来自当时可见raw字段
所有现金和股份变化都有ledger event
```

### 8.7 可复现门禁

- 结果含dataset snapshot ID；
- 结果含Git commit和dirty diff hash；
- 结果含策略、算子、rebalancer、税费和执行配置；
- 同输入连续运行两次，核心产物hash一致；
- 更改回测结束日期不得改变此前日期的信号和账户历史；
- 更改数据源版本必须生成新snapshot而不是覆盖旧结果。
- 运行前重算的各组件hash、行数和manifest全部匹配；
- 把新revision倒灌到源库后，旧snapshot的行选择和结果hash不变。

---

## 9. 测试体系

### 9.1 纯逻辑单测

必须新增或补强：

- 每个历史印花税区间的买卖费用；
- 2013前、2013～2015、2015后红利税；
- 分红派发后继续持有一年再卖；
- 部分卖出和FIFO递延税；
- 除息日应收、支付日可用；
- 10送3产生130股并最终清仓；
- 10转10混合现金事件；
- 送股除权日不可卖、上市日可卖；
- 配股行权/放弃；
- 调出日取消前日buy/add；
- 调出日sell继续执行；
- 退市警告、取消订单、最终结算；
- 停牌期间公司行为；
- 开盘缺失绝不使用收盘价；
- 一字涨停不买、一字跌停不卖；
- 开盘已在不利限价即使盘中开板也不假设开盘订单成交；
- 主板/ST/创业板/科创板/北交所和无限价日的历史边界；
- tick size舍入后的涨跌停价逐分对账；
- 限价schedule或前收缺失时strict fail-fast；
- hfq正式模式拒绝。
- `total_return_adjusted_pit`的纯现金、纯送转、混合、配股公式和截止日稳定性；
- snapshot冻结后追加新revision不改旧行选择，修改/删除已引用行被拒绝；

### 9.2 金标事件测试

选取至少以下真实类型，每例保存源记录和人工期望：

- 纯现金高分红；
- 纯送转；
- 现金+送转；
- 多次连续送转；
- 同日普通+特别分红；
- 配股；
- 吸收合并/换股；
- successor代码迁移且lot获取日不重置；
- 退市；
- ST及涨跌停期间的退出；
- 指数调入、调出与重新调入。

每例验证：

- 事件前后权益；
- 可用现金；
- 应收现金；
- settled/sellable/receivable shares；
- 成本和lot；
- 税款；
- 未成交订单；
- 最终可清仓。

### 9.3 对账测试

- raw+事件账本的总收益与供应商总收益调整序列对账；
- 本地 `total_return_adjusted_pit` 与供应商hfq只比较机械台阶和长期收益，禁止把hfq回灌为本地因子；
- 允许真实市场涨跌，但事件机械部分必须一致；
- 买入并长期持有的结果与人工现金流表一致；
- 选取若干证券与LEAN风格独立小模型交叉计算；
- 每年抽样不少于现金、送转、退市各若干事件。

### 9.4 回归和性能

- 当前118项测试作为起点，不能删除断言来过关；
- 所有bug先写失败测试再修；
- 300+500完整窗口内存不得因全市场数据线性膨胀；
- 禁止回测过程中逐股票逐日期N+1 SQL；
- snapshot连接全程只读；只有独立runtime/cache SQLite允许受控的算子缓存写；
- 性能优化前后核心结果逐值一致。

---

## 10. 分阶段实施计划

以下阶段严格按依赖执行。除标明可并行的工作外，前一阶段未验收不得进入下一阶段正式回测。

### Phase 0：冻结错误结果并建立治理

**目标**：防止现有探索结果继续被误用。

任务：

1. 本文成为唯一回测权威文档；
2. 删除过时收益表、运行PID、旧hfq方案和旧回测路线图；
3. CLI把 `valuation_basis=hfq` 标记为拒绝正式运行，最终移除或只留诊断命令；
4. 回测meta增加 `research_grade: exploratory|validated`；
5. 当前所有结果标记为exploratory；
6. 建立 `dataset_snapshot` 和 `data_quality_report` 设计；
7. 写死连续信号视图公式、hfq隔离、日级成交模型、二元门禁和snapshot版本规则。

验收：

- 文档引用只指向本文；
- CLI不会让用户误把hfq结果当正式结果；
- 没有质量报告时结果自动标记exploratory。
- 不再有需要开发者自行选择公式、成交假设、缺口容忍率或snapshot语义的未决项。

### Phase 1：安全修复和存量止血

**目标**：先修正在发生的双记和违规成交。

任务：

1. `_preload_cash_dividends`、送转预载按规范事件ID去重；
2. 存量9组/18行生成dry-run清理报告，经确认后事务化清理；
3. 数据库增加公司行为唯一约束；
4. 执行日重新检查指数成员资格；
5. 调出日取消pending buy/add，保留sell/reduce；
6. 禁止open缺失时close fallback；
7. 执行权益估值纳入停牌持仓last mark；
8. strict模式对不利开盘封板/一字板停止成交，限价输入无法确定时fail-fast；
9. 为上述每项增加回归测试。

验收：

- 重复事件为0；
- 调出证券不存在买入/加仓成交；
- 任意成交记录都能证明成交价是当时raw open；
- 现有测试和新增测试全绿。

### Phase 2：全市场主数据和数据模型迁移

**目标**：建立所有曾上市A股的数据骨架。

任务：

1. 新建普通任务分支并备份数据库；
2. 扩展`security_master`和永久`security_id`；
3. 增加代码历史表；
4. 全量拉取当前与历史退市证券主数据；
5. 用首末行情、交易所档案和第二来源交叉核验；
6. 扩展公司行为日期和类型字段；
7. 增加政策schedule、snapshot和质量报告表；
8. 所有snapshot管辖表改为append-only revision，增加阻止已引用行UPDATE/DELETE的数据库约束；
9. 编写幂等迁移、dry-run和回滚说明。

验收：

- 全市场master覆盖报告；
- 目标2023只历史成员master覆盖100%；
- delist覆盖不再为0；
- 干净库`--init-db`和旧库迁移都通过。

### Phase 3：全市场行情数据集

**目标**：保存全市场，但不扩大策略交易范围。

任务：

1. 以所有历史A股为回补代码源；
2. 从2007-01-01回补raw OHLCV、amount、status、ST、pre_close；
3. qfq/hfq作为独立诊断字段保存来源版本；
4. 分批、串行遵守BaoStock限制；
5. 每年/每证券生成覆盖报告；
6. 明确停牌行与缺失行；
7. 回灌来源limit up/down；来源不提供时用版本化交易规则逐日生成并对账；
8. 物化交易日历，保存生成器版本和逐日hash；
9. 建立rev唯一键、异常检查、断点续传，禁止原地覆盖旧revision；
10. 完成后checkpoint；是否VACUUM由磁盘和备份计划决定；
11. 冷数据Parquet仅作为后续优化，不阻塞准确性。

容量预算：

- 最终整库约5.3～6.2GiB；
- 工作期间至少12～15GiB空闲；
- 禁止同步运行其他SQLite写任务。

验收：

- 全市场1700万～1900万行量级合理；
- 每年质量报告通过；
- 缺口都归类为停牌、未上市、已退市或真实数据错误；
- 不存在静默缺失。

### Phase 4：官方历史成分修复

**目标**：使策略宇宙真正point-in-time。

任务：

1. 获取中证指数公司历史样本和调样公告；
2. BaoStock快照只作交叉验证和缺口线索；
3. 修复早期497/498成员问题；
4. 保存公告日、生效日、来源和版本；
5. 对临时调样、重新调入、代码变更建金标；
6. 按“官方公告已知+生效日启用；无公告则首次observed后下一交易日启用”裁决 `known_at`；
7. 生成每天成员数和区间连续性报告。

验收：

- 每个有效日沪深300=300、中证500=500；
- 无时间缺口、无污染；
- 回测区间内 `verified`成分security-day覆盖100%；
- 信号日和执行日成员资格测试通过。

### Phase 5：公司行为规范化、仲裁和全量回灌

**目标**：让raw账户的所有机械权益变化可解释。

任务：

1. 保留BaoStock原始14字段，包括payDate和stockMktDate；
2. 原始行先写source-record；
3. 规范化现金、送转和混合事件；
4. 完成同日普通+特别分红“相加”与重复行“二选一”仲裁；
5. 混合现金+送转使用完整除权关系验证，禁止只按现金反推；
6. 按§5.7生成本地price ratio，用hfq/raw因子台阶做独立对账，不读取hfq构造本地因子；
7. 回灌2006年至截止日；
8. 接入配股、合并、代码变化和退市事件；
9. 输出accepted/rejected/ambiguous报告；
10. ambiguous事件涉及 `E(t) ∪ H(t)` 时阻断整个strict运行；只有显式改变宇宙的exploratory实验才可剔除。

验收：

- 早期年度事件数量合理；
- 送转不再为0；
- 重复为0；
- 所有调整因子跳变已解释或明确拒绝；
- 金标事件全部通过。

### Phase 6：事件驱动账户重构

**目标**：正确区分经济权益和可用资产。

任务：

1. 引入现金应收、股份应收；
2. 引入统一lot队列，拆分settled/sellable/receivable shares；
3. 除权日、支付日、上市日分别处理；
4. 会计成本和风险参考成本分离；
5. 修复现金除息止损；
6. 修复零股清仓；
7. pending订单响应送转、退市和代码变化，successor迁移不重置lot获取日；
8. 建立逐日不变量检查；
9. 账户ledger记录每一笔状态变化。

验收：

- 合成事件权益连续；
- 应收不可提前消费或卖出；
- 所有持仓最终可结算；
- 无负现金、负股份和lot失配。

### Phase 7：历史税费模型

**目标**：准确反映2007年以来制度。

任务：

1. 用官方文件建立印花税schedule；
2. 建立过户费schedule；
3. 佣金改为profile配置；
4. 建立红利税历史schedule；
5. 红利税改为卖出时FIFO补扣；
6. 输出gross和individual_net两套曲线；
7. 每笔交易记录费用明细和政策版本。

验收：

- 所有政策边界日前后单测；
- 手工现金流案例逐分对账；
- 费用总额可由交易明细重算。

### Phase 8：严格执行器与退市

**目标**：完成日级Broker语义。

任务：

1. 正式Order对象和状态机；
2. 执行日宇宙/交易状态复核；
3. 无open不成交；
4. 实现§5.6保守日级成交模型、历史涨跌停schedule、tick舍入和无限价例外；
5. 停牌/开盘封板/一字板顺延和有效期；
6. 先卖后买与现金约束；
7. 退市warning/delisted事件；
8. 合并换股和代码变化；
9. 可选成交量参与率模型；
10. 回测末日未执行订单明确取消并报告。

验收：

- 订单状态转换覆盖测试；
- 所有取消/顺延有reason；
- 末端持仓和订单全部可解释。

### Phase 9：信号point-in-time与财务数据

**目标**：消除剩余研究数据泄漏。

任务：

1. 给所有算子增加价格口径声明；
2. 建立qfq临时白名单；
3. 按§5.7公式实现由raw+accepted事件动态构造的point-in-time连续信号窗口；
4. 调整成交量；
5. 财务数据按公告日可见；
6. 因子截面严格限定E(t)；
7. 算子缓存key加入dataset snapshot和normalization；
8. 不同截止日重建数据后做信号稳定性测试。

验收：

- 每个算子都有数据契约；
- 绝对价格逻辑不使用qfq；
- 财务数据无公告日前使用；
- 截止日变化不改历史信号。
- 代码依赖和运行追踪均证明本地连续视图未读取qfq/hfq字段或供应商adjustment factor。

### Phase 10：基准、产物与可复现

**目标**：使结果可比较、可审计、可重跑。

任务：

1. 补齐300/500价格和总收益基准；
2. 构造300+500组合基准；
3. 结果保存snapshot、commit、dirty hash；
4. 保存全部策略/执行/税费配置；
5. 质量报告随meta落盘；
6. snapshot在一致性读事务中固定ingested-at cutoff、选中revision、行数和组件SHA-256；
7. 正式运行只读snapshot、独立写runtime cache，运行前重验hash；
8. 同输入生成核心结果hash；
9. validated结果与exploratory结果分目录或标签。

验收：

- 总收益策略只与总收益基准比较；
- 同输入两次hash一致；
- 新增revision后旧snapshot重跑hash仍一致；
- 任一收益数字可追到数据和代码版本。

### Phase 11：分层验收回测

顺序不可跳过：

1. 单证券、单公司行为金标；
2. 5～10只事件密集证券一年；
3. 300+500一个调样窗口；
4. 2007～2012早期压力窗口；
5. 2013～2020中期窗口；
6. 2021～截止日近期窗口；
7. 2007-01-15～截止日全周期；
8. 同snapshot重复运行；
9. 税前/税后/费用敏感性分析；
10. 与独立实现或成熟框架风格小模型交叉验证。

任何阶段失败都回到相应数据或机制阶段，不允许为了“跑完”降低strict门禁。

### Phase 12：正式策略研究

只有Phase 11全部通过后才开始：

1. 固定训练/验证/样本外区间；
2. 先做单因子IC、分位收益、换手和衰减；
3. 再组合策略；
4. 参数选择只在训练集；
5. 验证集确认；
6. 样本外区间只打开一次；
7. 报告多重试验和策略淘汰数量；
8. 做费用、滑点、延迟、成分源和税务敏感性；
9. validated结果才能参与实盘决策。

---

## 11. 计划中的CLI和操作流程

命令名可在实施时微调，但职责必须保持：

```bash
# 公司行为只读审计（按年覆盖、重复和金额/来源异常；不写库）
python3 main.py --audit-corporate-actions \
  --corporate-action-start-year 2007 \
  --corporate-action-end-year 2026

# 全市场主数据
python3 main.py --backfill-security-master --scope all-a-history

# 全市场行情
python3 main.py --backfill-market-history \
  --scope all-a-history --start 2007-01-01 --end 2026-07-24

# 历史指数
python3 main.py --backfill-index-history \
  --indices 000300,000905 --verified-only

# 公司行为dry-run / 回灌 / 仲裁报告
python3 main.py --backfill-corporate-actions \
  --scope all-a-history --start-year 2006 --dry-run

# 当前已实现：显式单证券证据暂存（均不写旧 dividend_event）
python3 main.py --stage-corporate-actions \
  --corporate-action-codes 300024 --corporate-action-start-year 2007
python3 main.py --stage-legacy-corporate-actions \
  --corporate-action-codes 300024 --corporate-action-start-year 2007
python3 main.py --stage-akshare-corporate-actions \
  --corporate-action-codes 300024 --corporate-action-start-year 2007
python3 main.py --stage-akshare-rights-issues \
  --corporate-action-codes 600030 --corporate-action-start-year 2007
python3 main.py --stage-exchange-delistings
python3 main.py --materialize-corporate-actions

# 创建不可变数据快照
python3 main.py --create-dataset-snapshot \
  --cutoff 2026-07-24 --strict

# 金标验收
python3 -m pytest tests/backtest_gold -q

# 正式回测
python3 main.py --backtest dividend_cross_section \
  --snapshot a_share_20070115_20260724_v1 \
  --start 2007-01-15 --end 2026-07-24 \
  --strict --tax-profile individual-cn
```

正式任务操作要求：

- 所有写任务串行；
- 启动/结束后台任务写入`.local/WORKSTATE.md`；
- 回灌前SQLite在线备份；
- WAL checkpoint后再复制数据库；
- 任何清理先生成dry-run报告；
- 禁止在正式snapshot上原地覆盖；
- 数据和结果产物不提交Git，schema、报告模板和文档提交Git。

---

## 12. 每阶段交付物模板

每个Phase必须交付：

```text
1. 代码和迁移
2. 单元测试
3. 金标或集成测试
4. 数据质量报告
5. 性能/容量报告
6. 已知限制
7. 回滚方式
8. Git commit
9. 下一阶段准入结论：PASS / FAIL
```

质量报告至少列出：

```text
scope
date range
expected securities / actual securities
expected trading rows / actual rows
missing master
missing or duplicate bars
constituent gaps
corporate action accepted/rejected/ambiguous
unexplained adjustment jumps
unsupported delist/merger/rights events
future-known records excluded
tradeability rule coverage / unknown inputs
limit-up/down rejects and deferred notional
snapshot component row counts / hashes
gate result
```

---

## 13. Definition of Done

只有同时满足以下条件，才能声明“2007～2026的300+500历史回测准确可用”：

- 全市场数据已保存且有不可变snapshot；
- 300+500每天的历史成员经验证；
- 调出后无任何买入或加仓，包括前日遗留订单；
- master、上市、退市、代码生命周期完整；
- raw行情、停牌和缺失可区分；
- 现金、送转、配股、合并、退市均已处理或严格拒绝；
- 除息日、支付日、股份上市日时序正确；
- 零股可清仓；
- 红利税按卖出时最终持有期和历史政策结算；
- 2007年以来交易税费按日期正确；
- 风控不把公司行为机械缺口当亏损；
- 正式账户不使用qfq/hfq成交或盯市；
- `total_return_adjusted_pit`只由snapshot内raw+已仲裁事件按§5.7运行时构造，不读取供应商qfq/hfq因子；
- 每个执行日的交易状态、历史限价规则、tick舍入和raw open可得性均可证明；
- 所有算子有point-in-time数据契约；
- 主要基准为相匹配的总收益基准；
- 金标、对账、回归、可复现和分段压力测试全部通过；
- 每个结果能追溯到snapshot、Git commit、配置和质量报告；
- snapshot的行选择、摄取截止、行数和组件hash已冻结，运行期间只读，新revision不改旧结果；
- `E(t) ∪ H(t)`关键分母内未解释缺口为0；任一受影响证券/日期被拒绝时，完整300+500 strict运行FAIL，不靠剔除成分继续。

在此之前，系统可以继续开发和探索，但结果必须标记为 `exploratory`。
