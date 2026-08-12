# StockFu A 股回测：当前研究模式

> 文档状态：2026-08-10。旧的高可信 Raw 账户、独立公司行为账本和旧版 strict 账本方案已从主回测实现移除；仍有价值的风险提示保留在“研究经验”中。当前交易约束仍由宇宙与执行规则统一执行。**V2 修复前的回测数字已作废；修复后正式 canonical 验收见 §5。**

## 0. 当前口径

StockFu 是日频、A 股多头研究系统，目标是比较策略方向和因子稳定性，不把免费数据源包装成机构级历史复现。正式研究结果必须同时携带数据口径、代码版本、参数、费用、基准和数据缺口。

### 0.1 价格与公司行为

| 层 | 当前口径 | 说明 |
|---|---|---|
| 绝对值信号 | raw close + 税前分红 | 股息率、PE/PB 等不能用 qfq 分母，避免前视 |
| 收益与净值 | qfq 前复权 | 资金收益率含分红再投，接受供应商复权基准漂移 |
| 红利税 | baostock `dividCashPsAfterTax` | 研究近似，不重建持有期 FIFO 扣税账本 |
| 送转/拆股 | qfq 已调整 | 不在账户层重复处理 |
| 退市 | `outDate` + `exit_only` | 退市日清仓，不伪造终值 |
| 配股/合并/换股 | 暂不建模 | 研究结果必须标注这一限制 |

公司行为唯一正式来源是 `dividend_event`（baostock 回补并经过已知冲突裁决）；项目不再维护多源仲裁账本。引擎仍支持 `valuation_basis=raw/qfq/hfq`，默认 `qfq`。

### 0.2 时间、宇宙和执行

- 所有因子读取必须有 `as_of` 上界；历史宇宙使用沪深300/中证500时点成分，而不是今天的成分倒灌历史。
- T 日收盘生成信号，T+1 开盘执行；执行时检查上市状态、停牌、涨跌停、整手、滑点和费用。
- 默认基准是沪深300；策略结果只能与同窗口、同口径的基准比较。
- `cap_and_rank` 负责从横截面分数生成目标权重，rebalancer 负责组合层，engine 负责成交和账户。

### 0.3 数据与代码边界

```text
operator → strategy YAML → rebalancer → execution engine → artifacts
```

- 主库 `data/stockfu.db` 保存行情、分红和历史宇宙；算子结果位于独立的 `operator_cache.db`。
- 算子缓存可再生，指纹包含算子源码 hash；回测不得把缓存写回行情主库。
- 长窗回测使用 `CompiledStrategy.begin_run_cache()` 的滚动窗口预载，避免一次把全周期算子结果全部读进内存。
- 研究产物写入 `data/backtest/`，是可删除、可重算的本地工件，不纳入 Git。

### 0.3.1 固定样本区间与产物保留（V2）

从 2026-08-10 起，正式 V2 回测必须同时运行并保留以下三段；不能只跑全量段后把它当作稳定性验证：

| 区间 ID | 请求区间 | 说明 |
|---|---|---|
| `full` | 2013-01-01 → 2026-12-31 | 当前全量基线；实际终点由快照决定，当前数据会截断到 2026-08-04 |
| `2013-2019` | 2013-01-01 → 2019-12-31 | 早期市场阶段 |
| `2020-2026` | 2020-01-01 → 2026-12-31 | 近期市场阶段；实际终点由快照决定 |

三段使用同一数据快照、代码版本、alpha/portfolio/risk、费用、基准和候选池；每段独立初始化账户与历史状态，不把上一段的持仓或 `HistoryState` 带入下一段。默认 `observation_count=271`，每段的预热起点独立计算并以 2013-01-01 为下限（因此默认分别为 2013-01-01、2013-01-01、2015-01-01）。

每次 suite 必须使用新的 `run-*` 输出目录，目录结构为：

```text
data/backtest/v2-segments/run-<timestamp>/
├── suite.json
├── full/<variant>/<alpha_id>/
├── 2013-2019/<variant>/<alpha_id>/
└── 2020-2026/<variant>/<alpha_id>/
    ├── <alpha_id>.json                 # 可检索摘要/指标
    ├── <alpha_id>.checkpoint.json      # 完整 equity/trades/orders/risk/holding 状态
    └── <alpha_id>.checkpoint.json.audit.jsonl
```

旧的 `data/backtest/ten-strategies-long/`、`ten-strategies-daily/` 等产物不删除、不覆盖；新的分段 runner 也拒绝复用非空 suite 目录。checkpoint 是完整数据真源，summary 只做检索和汇总，audit 文件保留逐日审计链。

### 0.4 已知限制

复权数据可能漂移，红利税是源端近似，免费源的退市终值和特殊公司行为不完整，日线不能复现盘口排队。结果可用于方向比较、因子诊断和参数粗调；不可据此宣称精确税后年化或无风险实盘收益。

## 1. 标准回测流程

```bash
python3 main.py --list-strategies
python3 main.py --backtest STRATEGY --start 2007-01-04 --end 2026-07-21 \
  --valuation-basis raw --save
python3 main.py --factor-diag OPERATOR --start 2007-01-04 --end 2026-07-21
python3 main.py --update-backtests --strategies a,b --start 2007-01-04 --end 2026-07-21
```

### 0.6.1 标准绩效输出

每份报告至少包含：配置头、总收益/年化/期末权益、基准及超额、最大回撤/回本天数、Sharpe、Sortino、Calmar、交易笔数/换手、胜率、费用、分红、平均/最大仓位、涨跌停拒单和延迟订单。水下天数必须明确是相对初始本金还是历史高点。

## 0.6 研究经验与准入门禁

### 0.6.2 组合风控实验留下的结论

- 组合级敞口刹车在包含 2008 年的长周期中能显著降低回撤；单纯缩小单票权重但每天重新填满组合，不能降低总敞口。
- 回撤加仓门控在近年窗口看起来有效，但在长周期危机中会放大风险；不作为默认增强。
- 这些历史变体保留在 YAML 供复现，但旧数字不是当前 canonical 策略的验收结论。

### 0.6.3 滞回与低仓位实验

双总分滞回和 8 成仓属于低回撤版本，收益也同步下降，不能写成“收益增强”。

### 0.6.4 train/test 样本外筛查

全样本调参会把某段行情的风格暴露误认为 alpha。旧实验曾出现全样本优秀、test 明显衰减的情况，证明只看 full 不足以准入。

### 0.6.5 基准与风格偏差

红利/低波/价值策略与沪深300的风格并不相同；报告应同时考虑红利指数等风格基准。

### 0.6.6 新策略三段门禁（必须执行）

新增策略、改参数、改算子后，在认定结论前必须使用 `full`、`2013-2019`、`2020-2026` 三段固定区间。三段使用同一代码版本、数据快照、费用、基准和 `observation_count`，并同时保留完整产物。

全量段只回答“这套口径在可用数据上的总体表现”；两个子段用于识别行情阶段依赖。全量好看但任一子段超额反向、Sharpe 明显衰减或回撤失控，必须标记为过拟合/待验证，不进入正式保留集。修正风险配置后必须三段全部重跑，旧配置的 train/test 不能沿用。

当前行情库只覆盖 2013 年起，因此本门禁不能冒充 2007-2026 全样本门禁；若未来补齐 2007-2012，另行增加固定的 2007-2026 版本，不替换本三段基线。

### 0.6.7 第一批调研候选的历史预审

2026-08 的第一批 10 个策略曾先跑过一批 full/train/test，但部分 YAML 省略了 `portfolio_brake`，实际加载了引擎默认组合刹车。该批 train/test 结论已撤销；canonical full 统一显式设置 `portfolio_brake=0`，结果见下一节。旧的“保留/清除”快照不再作为验收依据。

### 0.6.8 `smart_beta_multi_factor` 风格排查

该策略 train 段小盘权重约 77%，2007–2013 年曾达到 87–94%；full/train 的高超额主要来自小盘风格 beta，而非已证明的多因子 alpha。它降级为参照策略，正式保留前仍需市值中性化并通过当前固定三段门禁。

### 0.6.10 第一批 10 个策略 canonical full

**口径**：2007-01-04 → 2026-07-21，4749 个交易日，raw，沪深300基准收益 +129.27%，历史沪深300/中证500时点成分宇宙，`cap_and_rank`，`max_gross=1.0`，单票 `max_w=0.20`，T+1 开盘卖优先。以下是 full 结果，不等同于样本外通过。

| 策略 | 总收益 | 年化 | 超额 | 最大回撤 | Sharpe | 交易笔数 | full 判定 |
|---|---:|---:|---:|---:|---:|---:|---|
| `low_beta_dividend` | 719.19% | 11.81% | 589.92% | 55.02% | 0.62 | 1304 | ✅ |
| `graham_defensive_value` | 725.49% | 11.85% | 596.22% | 68.65% | 0.57 | 1995 | ✅ |
| `smart_beta_multi_factor` | 710.68% | 11.74% | 581.41% | 59.57% | 0.56 | 1970 | ✅ 参照 |
| `fifty_two_week_high_cross_section` | 66.61% | 2.75% | −62.66% | 74.39% | 0.23 | 2283 | ❌ |
| `ts_momentum_trend` | −18.57% | −1.08% | −147.84% | 71.30% | 0.09 | 3263 | ❌ |
| `donchian_breakout_cross_section` | −94.21% | −14.03% | −223.48% | 97.95% | −0.34 | 4303 | ❌ |
| `small_cap_low_turnover` | 1191.76% | 14.54% | 1062.49% | 66.56% | 0.65 | 1368 | ✅ 待样本外 |
| `low_turnover_reversal` | 488.08% | 9.86% | 358.81% | 61.17% | 0.49 | 2881 | ✅ 待样本外 |
| `illiquidity_value` | 345.00% | 8.24% | 215.73% | 76.25% | 0.42 | 5228 | ✅ 待样本外 |
| `anti_lottery_defensive` | 639.00% | 11.20% | 509.73% | 63.47% | 0.56 | 3332 | ✅ 待样本外 |

7/10 的 full 总收益超过沪深300，但这只是本轮“完成 10 个全周期回测”的停止条件，不是 7 个策略已通过当前固定三段门禁。当前安全参考策略为 `graham_defensive_value`；`smart_beta_multi_factor` 仅作风格暴露参照。

### 0.6.11 V2 十策略三段筛选（2026-08-12，研究性 / 非 canonical）

基于 `run-...188679`（monthly/weekly，git-dirty 时执行，49/90 complete 后被 canonical 门禁拒）+ 新跑 canonical `value_ep_bp_v2 daily`（`run-20260812-112002-663595`）做的 V2 十策略筛选性三段分析。回测引擎代码在 188679 之后未改（后续提交 88e3083/01df060/d1cb934 仅涉及 scheduler/recommend），故结论用于「决定优化哪个策略」可信；**但 provenance 非 canonical（未绑 clean commit SHA），不得作为正式准入门禁结论**。坐实须 git clean 重跑三段。

各策略取最优频率 full 段结果：多因子(monthly, Sharpe 0.99)、价值(daily, 0.81)、高股息(monthly, 0.65)三段超额全正且有实质超额(+174~+395)，为唯一值得继续投入的三条；低波动(monthly, 0.59)方向稳但超额仅 +9（弱 alpha）；低Beta防御(monthly, 0.54)full 超额 +1 且 2013-2019 牛市跑输 29%，属防御风格 beta 非 alpha；52周新高/动量/反转/趋势/RSI 三段方向混乱或 full 即跑输，淘汰（印证 A 股短期动量弱、反转接飞刀）。

频率验证：value_ep_bp 随调仓频率单调改善（月 0.64→周 0.73→日 0.81，daily 换手 472% 反低于 weekly 814%）；multi_factor/dividend/low_vol 高频劣化、甜点为 monthly（low_vol weekly 已现门禁裂缝 --+）。multi_factor daily 子段因 OOM 缺失（4 因子 × 全量段 × 日度，3.6G 内存无 swap）。完整结果表见 `docs/SPECS/ten-strategies-results.md` 文末。

## 4. 结果解释与复现要求

回测结果必须保留 `data/backtest/run-tune-{strategy}-full.json.gz` 及 `.meta.json`，并记录运行时的 Git commit、参数、数据截止日和缓存命中情况。V2 正式三段结果必须遵守 §0.3.1 的 suite 目录契约；删除某策略缓存只会删除可再生工件，不代表删除了策略代码或验证结论。

如果结果和旧报告不一致，先检查：

- `valuation_basis` 是否一致；
- 基准和历史宇宙是否一致；
- 策略 YAML 是否显式设置 `portfolio_brake`、`max_gross`、`max_w` 和止损/止盈；
- 数据快照、算子源码 hash、引擎版本和起止日期是否一致；
- 是否把 full 结果误读成样本外结果。

长窗回测需要观察 RSS；算子缓存数据库很大不代表每次回测都应全量加载，滚动预载是当前实现的内存边界。

## 5. V2 因子评分与回测架构（2026-08 起核心落地）

V2 是与 V1 并存的新评分/回测架构，目标是替换 V1 的量纲契约：统一 0–100 因子分、四层解耦（评分 alpha / 组合 portfolio / 风险 risk）、严格点时与 prefix invariance。完整设计见 `docs/SPECS/factor-strategy-score-v2.md`，实现决策与疑难见 `docs/SPECS/v2-implementation-notes.md`。V2 修复前生成的结果不再作为验收依据。

### 5.1 范围与口径

- **价格与公司行为沿用 §0**（qfq 估值、raw 信号、T+1 开盘执行、dividend_event、涨跌停/费用/整手）；V2 只重写评分编排，记账/撮合复用 `engine.py` 已验证单元。
- 评分链路：raw 值 → 因子分（0–100，profile 映射）→ 策略分（alpha 加权）→ 目标仓位（portfolio）→ 风险覆盖（risk）。**同一 alpha 换 policy/risk，分数不变**。
- 时间协议：前 1/5 观察期不交易，后 4/5 formal；t 日评分只读 cutoff<t 的历史，先评分后更新状态，信号 t 日产生最早 t+1 成交；prefix invariance 为硬约束（§16.8）。

### 5.2 当前实现（核心 + 红利低波 vertical slice）

- 已落地：`stockfu/scoring/`（contracts/mappings/profiles/history/scorer）、`stockfu/strategy/`（alpha/portfolio/rebalancer/risk）、`stockfu/factors/raw/`、`stockfu/backtest/v2_engine.py` + `v2_run.py`、V2 参数/成熟门禁/确定性回归测试。当前只绑定 4 个 raw metric；未迁移算子见 `docs/SPECS/v2-unmigrated-operators.md`。
- canonical 因子档案为 `dividend_yield_ttm_v2`、`low_volatility_20d_v2`（hybrid 映射，当前行业字段不可点时追溯，因此不启用 `industry_history`）；旧的 `_v1` 档案仅保留作兼容复现。
- 当前不作为 V2 阻塞项：其余旧因子迁移和 V1 物理归档删除；旧实现档案见
  `docs/SPECS/v2-unmigrated-operators.md`。历史沪深300成分已按日生效过滤；行业点时比较、
  全部 V1 风险变体和旧 2007–2026 full 结果另作历史参考；当前正式研究门禁采用 §0.3.1/§0.6.6 固定三段。本阶段已提供 `no_overlay_v1` 基线
  与 `v1_core_v1` V1-inspired 核心风险版。

### 5.3 命令

```bash
python3 main.py --backtest-v2 dividend_low_vol_v2 \
  --start 2021-01-01 --end 2026-08-05 --history-origin 2018-01-01 \
  --observation-count 271 --codes hs300
# --codes: hs300（历史生效成分，按日过滤）/ 逗号列表 / 省略=全 A 股
# --portfolio-v2 / --risk-v2 可覆盖；--history-origin 默认 eval_start 前 5 年
# 正式可比回测必须固定 --observation-count；省略时仅按本次区间的 20% 做探索性切分。
# --checkpoint PATH：默认每 20 个交易日原子写入完整账户/历史/挂单/换手/risk 状态
# --resume PATH：从完整 checkpoint 继续；固定 observation-count 后可延长 --end
```

单次 `--backtest-v2` 仍保留给探索、调试和 checkpoint resume；正式研究使用固定三段编排入口：

```bash
# 当前十策略各跑三段（日度 deployment；约 30 次）
python3 main.py --backtest-v2-segments all \
  --codes hs300 --snapshot data/snapshots/stockfu-2ee50075f50c.db \
  --observation-count 271 --canonical

# 十策略 × 月/周/日 × 三段（约 90 次）；先 dry-run 查看计划
python3 scripts/run_v2_segment_matrix.py \
  --snapshot data/snapshots/stockfu-2ee50075f50c.db --codes hs300 --dry-run
python3 scripts/run_v2_segment_matrix.py \
  --snapshot data/snapshots/stockfu-2ee50075f50c.db --codes hs300 --canonical
```

分段入口不接受 `--start/--end`，避免调用方无意改变门禁区间；可用 `--segments full` 等只重跑单段的探索任务，但 `--canonical` 必须覆盖完整三段。矩阵输出的 `suite.json` 是批跑索引，单次 `<alpha_id>.checkpoint.json` 保留完整曲线、成交、订单、风控事件、持有批次、评分诊断和 manifest。

### 5.3.1 V2 记录层（recording schema 1）

V2 的 checkpoint schema 已升级为 3；记录层与评分审计分开保存，策略规则本身不因记录而改变：

- `state.equity_curve`：逐交易日权益、现金、持仓数量、总敞口、现金比例、换手金额、费用、分红、历史高水位、峰值回撤和相对初始本金的水下标记。
- `state.trades`：实际成交/公司行为入账；每笔调仓成交带 `order_id`、`order_reason`、成交股数、价格、费用、已实现盈亏。
- `state.order_events`：订单提交、成交、延期、拒绝、取消和无成交尝试；因此“止损触发但跌停未成交”不会被误记成止损成交。
- `state.risk_events`：逐票止损/止盈触发，以及组合刹车、市场 regime、波动率目标等聚合触发。
- `state.holding_periods`：按账户 FIFO lot 生成的已平仓批次，包含买入日、卖出日、持有交易日/自然日和退出原因；期末开放批次由账户状态重建。

最终 checkpoint manifest 直接携带这份小型 `metrics` 字典，同时报告换手金额/比例、平均和最长持仓、最大回撤金额与日期、本金水下 0/10/20/30% 天数比例、股息净收入、费用、胜率/盈亏比、止损触发数与实际成交/亏损金额。`underwater_*` 默认是“相对初始本金”口径；`max_drawdown` 和 `peak_drawdown_*` 是“相对历史权益高点”口径，不能混读。qfq 估值下股息已含在价格中，现金股息收入按 0 记录；只有 raw 估值并开启现金分红结算时才会产生独立现金股息事件。

### 5.4 修复后真实池 canonical 复验（研究结果，非收益承诺）

口径：`dividend_low_vol_v2`、沪深300历史点时成分并集按日过滤、2021-01-01 → 2026-08-05（快照数据到 2026-08-04，manifest 明确截断）、预热 2018-01-01、固定观察期 271 日、qfq 估值、月调 top15。两组均从干净提交 `98be076c2d2bcf1efc25d961dbe1b4d2608eafb7` 从头运行，`status=canonical`、`git_dirty=false`，绑定 lock SHA `ef8036cd…ab7` 与只读快照 `2ee50075…8cff`。修复前 `_v1` 的 +43.41% / +18.29% 结果已作废，不与下表混用。

| 风险配置 | 总收益 | 年化 | 最大回撤 | Sharpe | 超额(vs 沪深300 −1.08%) | 成交 |
|---|---:|---:|---:|---:|---:|---:|
| `no_overlay_v1` | +51.96% | +10.24% | 14.05% | 0.82 | +53.04% | 896 |
| `v1_core_v1` | +28.26% | +5.97% | 11.73% | 0.67 | +29.34% | 1753 |

两组 formal 期均为 1082 个交易日，首笔成交 2022-02-21，末笔成交 2026-08-04；股息 raw 缺失率 9.08%，低波为 0%。风控版触发 regime 631 次、组合刹车 47 次、止盈 15 次、止损 0 次。数字仅用于代码/数据口径下的研究复现，不代表策略优劣或实盘收益。

### 5.5 V2 引擎可用性结论

全量回归 `348 passed`，合成非空交易、月度调度、停牌延迟订单、风险缩放不累乘、raw 契约、终点采样、快照隔离、checkpoint/audit 恢复和 canonical fail-closed 均有覆盖。两组磁盘工件分别为 `data/backtest/v2-canonical-{no-overlay,v1-core}-98be076.json` 及同名 audit/log；独立报告 `data/backtest/v2-canonical-verification-98be076.json` 对 finalized、provenance、state/全部组件/output/run_id、1353 行 audit 链、快照 SHA/权限和 939 只候选池 fingerprint 共 14 类检查全部通过。**V2 在当前 vertical slice 内通过工程正确性验收，可用于可复现研究回测**；这不代表其余旧因子、行业点时比较、长周期策略研究门禁或实盘收益保证已经完成。
