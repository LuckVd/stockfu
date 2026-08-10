# V2 实现决策与疑难日志

本文件记录按 `docs/SPECS/factor-strategy-score-v2.md` 落地过程中的**范围决策、勘察发现、与设计的偏差、疑难及处置**。设计 §24 要求每步单独、不擅自发明兼容规则;凡自行决定处均在此留痕。

> 重要状态（2026-08-06）：本文件第 2 节的首轮回测数字和“核心逻辑正确”结论均为修复前快照，已作废。验收发现并修复了风险价格口径、风险目标复用、调仓调度、末日采样、延迟订单、估值回退、raw 契约和 checkpoint 完整性问题；以文末 §4 的修复后复验为准。

---

## 0. 范围决策(2026-08-06)

### 0.1 本次实现的边界

完整 §21 八阶段(33 因子迁移 + 52 策略 V1 归档 + portfolio/risk 全量迁移)在单次落地中不现实(量级数千行)。用户的验收目标聚焦于:**用 V2 架构复现一个红利低波策略、跑通 2021–2026 回测**。故本次落地范围:

- **忠实实现设计核心架构**(一个不落,严格按公式):contracts → mappings(fixed/中秩 ECDF/hybrid)→ profiles(指纹/版本)→ history state(self/market/industry + checkpoint)→ alpha(加权平均+coverage+门禁)→ V2 引擎(1/5 观察期、t+1 执行、先评分后更新、prefix invariance)→ portfolio policy。
- **因子聚焦到红利低波最小集**:`dividend_yield_ttm`(hybrid,绝对+市场+行业+自身四分量)与 `low_volatility_20d`(hybrid)。这两个恰好覆盖设计里**最复杂**的 hybrid 映射与多分量历史状态,足以验证架构正确性。
- **portfolio/risk**:最简组合政策(top_n_above_score 等权)+ `no_overlay` 基线，并补一套
  `v1_core_v1` V1-inspired 核心风险语义用于验证（止损、分段止盈、组合刹车、趋势 regime）。
- **暂不在本次做**(记录为后续阶段,设计本就分阶段):V1 完整归档与物理删除(§17,本次不删任何 V1 代码/YAML,新旧并存到 V2 验收通过后再走 §17 门禁)、其余 31 个因子迁移、完整 portfolio/risk 政策族、实时荐股/邮件/API 迁移。
- 算子边界单独记录在 `docs/SPECS/v2-unmigrated-operators.md`：当前仅绑定 4 个 raw metric，其中 `value` 只是 V1 对照兼容项；其余设计因子不进入 V2 active registry。

**取舍依据**:设计 §24 step 6 明确要求"先打通 vertical slice(一个 fixed + 一个 hybrid + 一个拆分因子)"。红利低波 = dividend_yield(hybrid)+ low_volatility(hybrid),是验证架构的合适 vertical slice,且能直接服务验收回测。准确性优先于覆盖广度。

### 0.2 勘察发现(2026-08-06)

| 项 | 结论 |
|---|---|
| `/opt/pro/stockfu` | `/data/stockfu` 的符号链接,同一 repo、同一 db |
| quote_snapshot 覆盖 2021-2026 | 1353 交易日、1962 股;qfq/raw/pe/pb 非空率≈100% |
| **market_cap 列** | **全表 NULL**(mcap=0/2530321)。size 因子不可用,但红利低波不依赖 size,**不影响本次** |
| dividend_event | 24068 条点时现金分红,ex_date 覆盖 1993–2026,字段齐全(announce/record/ex/pay + per_share_cash + after_tax) |
| security_master | 仅 801 行(自选池);全市场点时用 stock_basic(7190 行,含 listing/delisting/industry/is_st)与 index_constituent(63704 行,带 effective_from/to) |
| `factors.percentile` | 已是中秩 `(below+0.5·equal)/n·100`,与设计 §6.2 一致(可直接复用语义) |

### 0.3 复用边界(严格遵守 §3.3 "可以有条件复用")

**复用(经 BACKTEST 基线验证、点时正确,不重写)**:
- `stockfu/backtest/engine.py`:`VirtualAccount`/`Position`(`apply_action` 整百股+费用+移动加权成本+FIFO lots、`credit_dividend` 按持有期扣红利税、`adjust_for_stock_dividend` 送转调股数)、`settle_dividends`(公司行为顺序:先现金除息、后送转)、`stamp_duty_rate`、`_get_trade_price`、`_trade_calendar_days`、费用常量。
- `stockfu/services/factors.py`:`quote_series`(点时序列,带回测内存供给器)、`percentile`、`quote_model_for`、`price_column`。
- `stockfu/services/dividend.py`、`valuation.py` 的点时取数接口。

**全新写**(V2 契约,不沾旧 score 语义):
- `stockfu/scoring/`(contracts/profiles/mappings/history/checkpoint)
- `stockfu/strategy/`(alpha/portfolio/risk 接口)
- `stockfu/backtest/v2_engine.py`(批量逐日评分编排;不复用 engine.py 的 per-code analyze + debounce + 风控耦合编排)
- `stockfu/factors/raw/`(纯 raw 计算器,返回 RawFactorObservation,不输出 score)

### 0.4 待续

后续步骤的关键决策、偏差、疑难按日期追加到本文件相应小节。

### 0.5 范围调整：暂不把行业列为验收目标(2026-08-06)

本阶段先不依赖点时行业数据，也不把行业比较/行业中性作为 V2 vertical slice 的完成条件；下一阶段优先处理点时股票池。现有 `industry_history`、行业状态和 `max_industry_weight` 代码保留为可选能力；旧 `_v1` profile/回测口径不静默改写，以保证已有结果可复现，canonical `_v2` profile 明确禁用行业历史分量。

如果后续要让某条策略完全不考虑行业，应新建 profile/alpha 版本，移除 `industry_history` 并明确关闭行业权重上限，再单独重跑基线；本次已按此规则建立 `_v2` 配置，不能直接修改现有 `v1` 配置。

### 0.6 验收口径调整：以可用性验证为主(2026-08-06)

- **不要求全量因子迁移**：当前 active raw metric 足以验证 V2 的评分、历史状态、alpha、组合和执行链路。旧因子保留 V1 实现，并在 `docs/SPECS/v2-unmigrated-operators.md` 记录来源、旧口径、参数和迁移状态；未迁移不等于删除或静默复用旧 score。
- **持久化以完整 checkpoint 为目标**：不做“只有摘要的轻量缓存”来冒充断点。V2 checkpoint
  每日原子写入完整可续跑状态：历史评分状态、账户现金/应收/费用/分红、持仓 lot 与止盈状态、
  待执行订单、组合目标、rebalancer、risk 状态、净值/成交/宇宙统计和 raw 诊断计数；固定
  `observation_count` 后允许只延长 `eval_end`。单独的审计日志不是本阶段阻塞项。
- **风险是当前硬目标**：V2 risk 必须参考 V1 的实际风险语义，至少覆盖止损、止盈/分批止盈、组合回撤刹车、市场 regime/总敞口等已在 V1 使用的核心能力，并保持不修改 strategy_score 的不变量。
- **验证口径**：本阶段先固定 `2021-01-01 → 2026-08-05`，预热 `2018-01-01`、
  `observation_count=271`、历史沪深300点时成分池，分别验证 `no_overlay` 基线和
  `v1_core_v1` 风险版能完整跑通，并做断点/不中断逐位一致性检查。不把 V1 结果当标准答案，
  也暂不执行 2007–2026 或三跑门禁；正式报告列收益、回撤、Sharpe、超额、成交和风控触发次数。

---

## 1. 实现记录(2026-08-06)

### 1.1 已实现模块

| 层 | 文件 | 设计节 |
|---|---|---|
| 契约 | `stockfu/scoring/contracts.py`(三类 Observation + Enum + canonical fingerprint) | §5 |
| 映射 | `stockfu/scoring/mappings.py`(fixed 线性插值 / 中秩 ECDF / hybrid 加权收缩 / `ecdf_score_sorted`) | §6 |
| 档案 | `stockfu/scoring/profiles.py`(FactorProfile + 校验 + 指纹 + registry) | §7 |
| 历史 | `stockfu/scoring/history.py`(self/market/industry rolling + 采样日 + checkpoint) | §8/§14 |
| 评分器 | `stockfu/scoring/scorer.py`(FactorScorer,market/industry 当日 sorted 缓存) | §4/§9.3 |
| 原始因子 | `stockfu/factors/raw/{dividend,volatility}.py` | §11/§12 |
| alpha | `stockfu/strategy/alpha.py`(加权平均 + critical/coverage 门禁) | §10 |
| 组合 | `stockfu/strategy/portfolio.py`(top_n 等权 + 容量门禁) | §13/§19 |
| 风险 | `stockfu/strategy/risk.py`（no_overlay + `v1_core_v1` V1-inspired 核心语义） | §19 |
| 引擎 | `stockfu/backtest/v2_engine.py`(逐日编排 + 时间协议) | §9/§15 |
| 入口 | `stockfu/backtest/v2_run.py` + `main.py --backtest-v2` | — |
| 配置 | `configs/{factor_profiles,alphas,portfolio_policies,risk_policies}/*.yaml` | §7/§19 |
| 测试 | `tests/test_scoring_mappings.py`、`test_scoring_history.py`、`test_v2_prefix_invariance.py`、`test_v2_protocol.py`、`test_v2_engine_correctness.py`、`test_universe.py` | §22 |

补充：V2 已实现完整 checkpoint/resume。checkpoint 采用 JSON 原子替换和配置指纹门禁，
保存历史评分状态、账户现金/应收/费用/分红、持仓 lot 与止盈状态、待执行订单、组合目标、
rebalancer、risk 状态、净值/成交/宇宙统计和 raw 诊断计数；固定 observation_count 后可延长
eval_end。风险强制退出优先于 portfolio 最小持仓锁。

### 1.2 实现中发现并修复的 bug

1. **sample_flags 键名不匹配**:`history_specs` 用全名(`self_history`),`HistoryState.update` 检查短名(`self`)→ 历史完全不积累(hist_n 全 0)。修:引擎用 `_COMP_SHORT` 映射。
2. **scale_buys_to_cash 解包**:`buys` 是 4-tuple(`code,tw,px,source`),解包写成 3。
3. **manifest 多余 `.alpha`**:`V2RunConfig.alpha` 是 `AlphaDefinition` 非 aggregator。
4. **observation_count 随 eval_end 变 → 违反 prefix invariance(§16.8)**:`obs_count=ceil(0.2·len(eval_dates))` 使延长结束日改变 formal_start。修:加 `observation_count` 显式参数,固定则 prefix invariant。这是设计 §9.4「canonical 评价期」的要求——观察期应是 canonical 固定的,不随每次回测终点重算。
5. **停牌/缺数据日持仓估值跳 0**:`acct.equity(close_q)` 对当日无收盘的持仓(停牌/数据末日)`prices.get(c, 0.0)` 取 0 → 单日权益暴跌(实测 2026-08-05 -39%)。修:维护 `last_close`,估值用 `{**last_close, **close_q}` 沿用上一交易日 close(同 engine.py 主线)。修复后 30 股全区间由 -12% 修正为 +44.6%。
6. **raw missing_rate 诊断未分期(§15 分区报告 / §22.4 分期统计)**:`raw_missing`/`raw_total` 曾是**全期单一计数器**(预热+观察+formal 每天累计),却被同时喂给 `observation_summary` 和 `formal_summary` → 两期 `missing_rate` **完全相同**,且分子分母混入预热期数据。违反 §15「observation/formal 分别报告」与 §22.4「每期分别输出 missing rate」。修:计数器改为 `{metric: {"obs":.., "formal":..}}` 分桶,raw 循环按 `t ∈ obs_set / formal_set` 计期(预热期 `period=None` 不计入任一诊断);`_raw_summary` 增返 `raw_total` / `missing_count` 绝对值供审计。加回归测 `test_raw_summary_split_by_period` 锁住两期独立(formal raw_total > observation;旧的全期计数器会使两者相等而失败)。注:不影响净值/交易,仅诊断报告口径。

### 1.3 性能优化

- **eval 期 ecdf 大池重复 sort**:每只股票的 market 分量(~数万样本)逐次 `sorted()`,300 股/天 → ~30s/天。修:`ecdf_score_sorted`(bisect 不重 sort)+ scorer 缓存当日 sorted market/industry(同 cutoff 全市场共用一份)。eval 期提速 ~15×。
- 预热期不评分(只 raw + history update);复用 engine.py 列式预载(qfq/raw/分红全字段零 DB)。
- raw profile 参数现在按 `raw_metric_id` 传入 calculator，并写入 manifest；同一 raw metric 被不同参数 profile 重复引用时直接拒绝，避免静默使用默认窗口。
- formal 首日参与 rebalance 日历；观察期最后一天不再作为前一换仓日。

### 1.4 偏差与简化(均记录,非偷工)

| 项 | 简化 | 设计要求 / 后续 |
|---|---|---|
| 候选池 | V2 `--codes hs300` 使用沪深300历年成分并集(939 只候选)并按日 `effective_from/to` 过滤，回测 2021–2026 | 复用 V1 的 `UniverseContext`/`index_universe`；本次完成入口接线、manifest 规则记录和回归验证。行业仍不作为本阶段验收依赖。 |
| 行业分类 | canonical `_v2` profile 不启用 `industry_history`；旧 `_v1` profile 仅作兼容复现 | 点时行业另立任务；当前结果不得宣称点时行业正确 |
| 风险政策 | `no_overlay` 为基线；`v1_core_v1` 已接入止损、分段止盈、组合刹车、趋势 regime、总敞口 cap，状态可 checkpoint/resume | 未迁移全部 V1 风险政策族（ATR/复杂质量门控等）不作为本阶段门禁 |
| 分红 | `credit_dividends=False`(qfq 已含分红再投) | raw 模式 + 红利税账本作为可选 |
| 因子集 | dividend_yield + low_volatility 两因子 | 其余 31 因子按 §21 阶段3 逐个迁移 |
| 全市场池 | 1960 只可跑(优化后 ~15-20min),首版用 hs300 平衡速度 | 全市场为 canonical 候选 |

### 1.5 修复前验证结果（已作废）

首轮定向测试和小池 smoke 曾全部通过，但没有覆盖真实成交、风险价格口径、月度调度和末日采样；因此当时的 `263 passed`、收益数字和“可用”结论均不能作为验收证据。修复后的验证清单见 §4。

---

## 2. 首轮回测结果（修复前，已作废）

`dividend_low_vol_v1` 的 +43.41% / +18.29% 两组数字来自未通过正确性验收的实现。问题包括：risk 使用 raw close 与 qfq 成本比较、风险缩放目标累乘、月度 policy 被每日调仓、非月末终点被错误采样、当前行业分类被当作历史、延迟订单丢失、停牌估值归零以及 checkpoint/raw 契约不足。旧结果与工件不得继续引用。

修复后的 canonical 配置是 `dividend_low_vol_v2`；新的 manifest、成交和绩效已记录在 §4，不沿用本节数字。

### 2.1 修复前结论（撤销）

“V2 引擎核心逻辑正确、可以使用”的旧结论撤销；修复后的工程验收结论和真实股票池复验以 §4 为准。

### 2.2 复现命令（修复后）

```bash
cd /opt/pro/stockfu
python3 main.py --backtest-v2 dividend_low_vol_v2 \
  --start 2021-01-01 --end 2026-08-05 --history-origin 2018-01-01 \
  --observation-count 271 --codes hs300
```

---

## 3. 运维记录(2026-08-06 本次 goal)

### 3.1 operator_result 孤儿缓存清理

勘察 `operator_cache.db`(22.7GB、清理前约 7850 万行)实际存在的算子 vs `operator` 注册表(23 个 known):**唯一孤儿为 `downside_skewness`**(3,497,490 行)——该算子已从 V1 注册表下线(设计 §12 迁移项),历史缓存永不命中。其余 17 个算子(low_volatility 1550 万、value 815 万、dividend_yield 780 万、graham_value 686 万、low_beta 679 万 等)均被 active 策略使用,保留。

执行 `cleanup_operator_results()`(`stockfu/ai/operators/seed.py:258`,`DELETE ... WHERE operator_id NOT IN (23 known)`)删除 **3,497,490 行,耗时 1116s**——WAL 模式下 7850 万行全表扫 DELETE 远慢于函数注释按 5.6M 行预估的「十几秒」。清理后 `downside_skewness` count=0,17 个在用算子缓存完好。

**边界(严格遵守 §17 分阶段)**:带旧 score 量纲但 V1 active 策略仍在用的算子缓存**保留**——设计 §17 阶段 8(V2 验收 + 归档门禁通过后)才整体清除「不迁移旧 score」;本次只清真正不能用的孤儿(算子已下线、永不命中)。库主文件 22.7GB 未缩:sqlite DELETE 不释放空间给 OS(需 VACUUM),但空闲页后续写入复用,不影响正确性。

### 3.2 top5 全量回测

按 `docs/BACKTEST.md` §0.6.10 canonical full 的 **sharpe 排序**选取 top5(仅作重跑对象选取,**非优劣认定**;§0.6.6 三跑门禁另立):

| # | 策略 | sharpe | §0.6.10 判定 |
|---|---|---:|---|
| 1 | small_cap_low_turnover | 0.65 | ✅ 待样本外 |
| 2 | low_beta_dividend | 0.62 | ✅ |
| 3 | graham_defensive_value | 0.57 | ✅ 当前安全参考 |
| 4 | anti_lottery_defensive | 0.56 | ✅ 待样本外 |
| 5 | smart_beta_multi_factor | 0.56 | ⚠️ 参照(§0.6.8 小盘风格 77-94%) |

口径复刻 §0.6.10:2007-01-04→2026-07-21(4749 交易日)、沪深300/中证500 时点成分宇宙、`raw`、`--save`。串行跑完(06:52→07:56,~64 分钟),**5/5 全部精确复现 §0.6.10 表**(总收益/年化/最大回撤/Sharpe/交易笔数逐项一致):

| 策略 | 总收益 | 年化 | 最大回撤 | Sharpe | 交易 |
|---|---:|---:|---:|---:|---:|
| small_cap_low_turnover | 1191.76% | 14.54% | 66.56% | 0.65 | 1368 |
| low_beta_dividend | 719.19% | 11.81% | 55.02% | 0.62 | 1304 |
| graham_defensive_value | 725.49% | 11.85% | 68.65% | 0.57 | 1995 |
| anti_lottery_defensive | 639.00% | 11.20% | 63.47% | 0.56 | 3332 |
| smart_beta_multi_factor | 710.68% | 11.74% | 59.57% | 0.56 | 1970 |

这同时验证两点:① 当前代码 + 数据下 V1 canonical 可精确复现;② §3.1 的孤儿缓存清理(downside_skewness)未影响任何 active 策略结果。产物 `data/backtest/run-20260806-0{65221,70228,71451,72621,73935}.json.gz`。**注**:smart_beta_multi_factor §0.6.8 已查明超额主要来自小盘风格 beta(非 alpha),仅作参照。此处复现 ≠ 三跑门禁通过(§0.6.6)。

---

## 4. 正确性修复与复验（2026-08-06）

> 本节结论已被 §4.1/§4.2 取代：第二轮验收提出的 4 个阻塞项与 2 个未覆盖项
> 已在 §4.1 修复，§4.2 为修复后的真实复验。本节保留作修复历史。

本轮整改的实现要点：

1. risk overlay 统一使用账户估值口径（qfq/配置的 valuation basis），保留 ideal target，避免风险缩放在连续交易日累乘；月度 portfolio policy 只在调度日重算，风险目标变化才触发额外订单。
2. 未成交且规则为 `defer` 的订单跨日保留；停牌持仓使用上一可用收盘价估值；末日没有后继边界时不虚构月末/周末采样。
3. raw observation 强制校验 metric、unit、as_of、source_max_date、available_at、invalid/value 组合和有限值；checkpoint schema 升级并加入 state checksum，旧不完整工件拒绝恢复。
4. 建立 `_v2` 因子/alpha/portfolio 配置，移除无法点时追溯的行业历史分量；`_v1` 配置保留为兼容复现，不静默改写。
5. 新增真实交易语义的合成引擎测试：月度不日调仓、停牌延迟订单、非空 checkpoint/resume 逐位一致，以及风险缩放不累乘、采样终点和 raw 契约回归。

复验结果：全量 `pytest -q` 为 **269 passed, 2 warnings**。`dividend_low_vol_v2` 在沪深300历史点时成分池、2021-01-01 → 2026-08-05、预热 2018-01-01、固定观察期 271 日下，`no_overlay_v1` formal 1082 日、896 笔成交、总收益 +51.96%、年化 +10.24%、最大回撤 14.05%、Sharpe 0.82、超额 +53.04%；`v1_core_v1` formal 1082 日、1753 笔成交、总收益 +28.26%、年化 +5.97%、最大回撤 11.73%、Sharpe 0.67、超额 +29.34%。两组均从非空的 2023-12-29 checkpoint 延长到终点，最终 checkpoint schema=2 且含 state checksum；合成测试另外逐位比较了不中断/恢复结果。

这证明本次垂直切片在当前数据快照和配置下通过工程正确性验收；仍不等同于全量因子迁移、点时行业比较、2007–2026 三跑门禁或收益保证。

## 4.1 第二轮验收阻塞项修复（2026-08-06）

针对 GPT-5.6-sol 第二轮验收提出的 4 个阻塞项与 2 个未覆盖项，本轮整改：

1. **deferred 挂单与目标撤销一致性（阻塞①）**：`pending_orders` 只被 `update` 覆盖同一代码，目标撤销（代码从 ideal/last_target 消失且未持仓）时旧买入挂单残留，停牌解除后照常成交、下一调仓日再卖出。修复：每次决策（should_decide）在合并新订单前，删除与最新 risk-adjusted target 不一致的遗留挂单（目标→0 的买入挂单取消；target=0 的卖出挂单保留；risk 目标变化的旧挂单作废由新订单覆盖）；resume 恢复后同样同步一次。合成回归：`test_deferred_buy_cancelled_when_target_removed`（撤销后零成交）与 `test_deferred_sell_survives_when_target_unchanged`（卖出挂单跨决策日保留），修复前均失败。
2. **checkpoint identity 含 raw 算法指纹（阻塞②）**：`manifest` 此前只有 raw_params，无 raw 算法名/指纹，替换 raw computer 后身份不变。修复：`v2_run.RAW_COMPUTERS` 注册表增加算法名（`RawComputerSpec(fn, algo)`），`build_v2_config` 计算 `raw_fingerprints[metric] = raw_fingerprint(metric, algo, params)` 进入 `V2RunConfig.manifest()["raw_metric_fingerprints"]` → checkpoint identity 自动包含；参数变化同样改变指纹。旧 checkpoint（无 raw 指纹信息）恢复时被正确拒绝。
3. **checkpoint 持久化完整 run manifest（阻塞②）**：工件除 `config_fingerprint`（opaque hash）外新增 `manifest` 字段（设计 §14「每次运行必须保存」），含全部指纹/口径/数据覆盖，可人工审计；`config_fingerprint` 与工件内 manifest 去掉 eval_end 后重算一致（测试锁定）。
4. **raw 契约校验加强（阻塞③）**：`_validate_raw_observation` 新增 asset 必须等于请求 code、`valid=True` 必须有 raw_value、raw_fingerprint 非空、invalid 必须带 missing_reason 四项硬校验（原已覆盖 metric/unit/as_of/source_max_date/available_at/invalid 带值/有限值）。
5. **数据截断检测与披露（阻塞④）**：交易日历预埋到 2026-12-31，但库行情（quote_snapshot/index_quote_daily）实际只到 **2026-08-04**；旧行为在请求终点（2026-08-05）跑无行情伪末日，equity 用 last_close 兜底、checkpoint last_completed 超前。修复：引擎用预载行情并集的最大日期作为 `data_end`，`data_end < eval_end` 时把 eval 日历截断到 `data_end`（不跑无数据日），并在 `manifest.data_coverage` 披露 `requested_eval_end / effective_eval_end / data_end / truncated`。旧 checkpoint（last_completed=2026-08-05）因此被拒绝，需重跑。
6. **§15 分数/成熟度/分位诊断（未覆盖项⑤）**：引擎在评分循环按观察/正式期收集 strategy_score 样本、effective_coverage、因子分 0/100 钳制计数、成熟度分布与首个成熟日（全部进 checkpoint 支持续跑），期末汇总为 `score_diagnostics`：score P01/P05/P50/P95/P99、0/100 饱和比例、横截面唯一值比例、score_coverage 均值、factor_clamp_rate、factor_maturity 分布、maturity_delay_days（formal 首日到首次成熟）、观察期分数均值/标准差；同步写入 manifest（§15 结果摘要）。
7. **完整 prefix invariance（未覆盖项⑤）**：新增 checkpoint 文件级前缀一致测试：短跑到 T1 的 checkpoint state 与长跑到 T2 的 checkpoint state 中截至 T1 的部分逐字段一致（self/market/industry 历史序列、equity_curve、trades、universe_sizes、score_samples 前缀；raw 累计计数 obs 相等、formal 单调不减）；resume 后 `score_diagnostics` 与不中断运行逐位一致。

复验结果（真实数据，见 §4.2）：全量 `pytest -q` 为 **279 passed, 2 warnings**；两组回测数字与上轮一致，终点截断到 2026-08-04 并在 manifest 披露。

## 4.2 修复后真实复验（2026-08-06）

`dividend_low_vol_v2`，沪深300 历史点时成分池，请求 2021-01-01 → 2026-08-05（**实际数据到 2026-08-04，引擎截断**），预热 2018-01-01，固定观察期 271 日，从头重跑（旧 checkpoint 因 identity 变化被正确拒绝）：

- `no_overlay_v1`：formal 1082 日、896 笔成交、总收益 +51.96%、年化 +10.24%、最大回撤 14.05%、Sharpe 0.82、超额 +53.04%；`manifest.data_coverage.truncated=True`，`effective_eval_end=2026-08-04`，末单 2026-08-04。
- `v1_core_v1`：formal 1082 日、1753 笔成交、总收益 +28.26%、年化 +5.97%、最大回撤 11.73%、Sharpe 0.67、超额 +29.34%；同样截断披露。

与上轮（2023-12-29 checkpoint 续跑、请求终点 2026-08-05）相比：成交笔数与收益指标完全一致（08-05 无行情无成交，估值兜底使净值不变），末单日从伪末日提前到真实数据末日 2026-08-04。`score_diagnostics`（§15）实测：score n=324234、p01=6.65/p05=14.82/p50=47.41/p95=78.74/p99=86.41、0/100 饱和 0%、横截面唯一值比 99.88%、score_coverage 均值 0.9322、factor_clamp_rate=0.0001、maturity 分布 mature 606345/partial 167564/immature 36969、maturity_delay_days=0（预热 3 年使 formal 首日即成熟）、观察期分数 mean=39.68/std=20.74。

**验收结论**：四个阻塞项与两个未覆盖项均已修复并有回归测试锁定；通过工程正确性验收。仍不等同于全量因子迁移、点时行业比较、2007–2026 三跑门禁或收益保证。

## 4.3 第三轮审查修复（2026-08-06）

第三轮独立审查（GPT-5.6-sol）确认 §4.1 多数修复成立，但提出 3 个剩余阻塞，本轮修复：

1. **raw 算法真实绑定（阻塞①）**：
   - 此前 `raw_fingerprints` 是调用方声明值，直接替换 `raw_computers` 的 callable 并保留声明指纹后 checkpoint identity 不变。修复：新增 `raw_computer_bindings`（每个 metric 的**函数源码指纹** `fn_source_fingerprint()`），进 manifest/checkpoint identity；`V2RunConfig.__post_init__` 用实际 computer 函数重算绑定，与声明不一致立即拒绝（替换 callable 无法绕过）；替换 callable 且伪造 bindings 时 identity 仍变化 → resume 拒绝。
   - 观测指纹一致性：`_validate_raw_observation` 在声明指纹非空时要求每条观测的 `raw_fingerprint` 与声明一致（阻断「计算器换了算法但声明没更新」的静默错配）。
   - 断点回测（checkpoint/resume）必须提供完整 `raw_fingerprints`，空指纹不再允许。
   - 新增真实反例回归：`test_replacing_raw_callable_while_keeping_declared_fingerprint_is_rejected`、`test_replacing_raw_callable_changes_checkpoint_identity`、`test_observation_fingerprint_mismatch_with_declared_is_rejected`。
2. **checkpoint 落盘完整 run manifest（阻塞②）**：提取 `build_manifest()`（最终结果与 checkpoint 共用），工件 `manifest` 字段现在包含运行结果字段：`run_id`、`formal_start`、`first/last_trade_date`、`n_trades`、`trades`、`universe`、`risk_metrics`、`data_coverage`、`checkpoint` 来源、`score_diagnostics`、`daily_audit` 摘要，外加全部配置指纹（§14「每次运行必须保存」）；`config_fingerprint` 仍是纯配置身份（不含运行字段），测试锁定两套语义。
3. **诊断口径修正（阻塞③）**：
   - `score_coverage` / `factor_clamp_rate` / `factor_maturity` 全部按 observation/formal 期分桶，不再混期。
   - `unique_ratio` 改为**每日横截面唯一值比例的均值**（§15「横截面唯一值比例」），附 `unique_ratio_days`；观察期同样输出。
   - 新增逐日审计 `daily_audit`：每 eval 日一条，含日期/期别 + 每票 strategy score、每 profile factor score/maturity/coverage、每 metric raw 值（round 控体积），随 checkpoint 续跑累积，V2Result 返回；prefix 测试逐日比较（短跑审计 == 长跑审计中截至 T1 的前缀），resume 后与不中断运行逐位一致。
   - 清理 ruff unused import（tests/test_v2_risk.py）。

复验结果（§4.4）：全量 `pytest -q` 为 **282 passed, 2 warnings**；真实回测数字与上轮一致，诊断分期/横截面唯一值/逐日审计生效，v1-core 落盘真实 checkpoint 含完整 run manifest。

## 4.4 第三轮修复后真实复验（2026-08-06）

`dividend_low_vol_v2`，沪深300 历史点时成分池，请求 2021-01-01 → 2026-08-05（实际数据到 2026-08-04，引擎截断），预热 2018-01-01，固定观察期 271 日，从头重跑；v1-core 带真实 checkpoint 落盘（checkpoint_every=250）：

- `no_overlay_v1`：formal 1082 日、896 笔成交、总收益 +51.96%、年化 +10.24%、最大回撤 14.05%、Sharpe 0.82、超额 +53.04%；`data_coverage.truncated=True`、`effective_eval_end=2026-08-04`。
- `v1_core_v1`：formal 1082 日、1753 笔成交、总收益 +28.26%、年化 +5.97%、最大回撤 11.73%、Sharpe 0.67、超额 +29.34%；同样截断披露；真实 checkpoint 工件 `/tmp/v2_verify_ckpt.json` 含完整 run manifest（run_id/data_coverage/formal_start/n_trades/trades/universe/risk_metrics/score_diagnostics/daily_audit 摘要 + 全部配置指纹）。
- 诊断实测（obs/formal 分桶）：formal score n=324234、p01=6.65/p05=14.82/p50=47.41/p95=78.74/p99=86.41、0/100 饱和 0%、横截面唯一值比例 100.0%（每日均值，1082 日）、coverage 均值 formal 0.9339 / obs 0.9251、clamp_rate formal 0.0001 / obs 0.0004、maturity formal mature 492960/partial 126063/immature 29445、obs mature 113385/partial 41501/immature 7524、maturity_delay_days=0；obs 分数 mean=39.68/std=20.74/唯一值 99.9988%；逐日审计 1353 天（obs 271 + formal 1082），短跑/长跑与 resume/不中断逐位一致。

**验收结论**：第三轮审查的 3 个剩余阻塞与 ruff 问题均已修复并有真实反例回归锁定；通过工程正确性验收。仍不等同于全量因子迁移、点时行业比较、2007–2026 三跑门禁或收益保证。

## 4.5 第四轮审查修复（2026-08-06）

第四轮独立审查确认 §4.3 正向证据成立，但提出 3 个验收阻塞与 1 个性能风险，本轮修复：

1. **运行入口强制绑定 raw callable（阻塞①）**：`raw_computer_bindings` 此前只在 `V2RunConfig.__post_init__` 校验，配置构造后替换 `cfg.raw_computers[metric]`（新函数仍返回原声明指纹）可绕过。修复：`run_v2_backtest` 入口用实际 computer 重算 fn 源码绑定并强制校验（不匹配立即拒绝），且**以真实绑定覆盖 cfg.raw_computer_bindings**——checkpoint identity 始终反映实际使用的函数。反例回归：`test_run_entry_rejects_post_construction_callable_swap`（构造后替换、观测指纹不变 → 运行入口拒绝）。
2. **中途 checkpoint 日期（阻塞②）**：`build_manifest` 此前用 `dates_all[-1]` 写 `checkpoint.last_completed_date`，首日工件会谎称已到终点。修复：`build_manifest(last_completed, final)` 接收实际保存日，`save_checkpoint` 传本次保存日；反例回归 `test_intermediate_checkpoint_manifest_uses_save_date`（spy 每次落盘：首日保存 state=manifest=2024-01-02，非 01-11）。
3. **§14 可复现字段补齐（阻塞③）**：
   - `git`：运行开始取 `rev-parse HEAD` + `status --porcelain` dirty 标志（一次，缓存）。
   - `data_coverage.data_snapshot_id`：关键行情表（quote_snapshot/index_quote_daily/etf_quote_daily）`(max_date, row_count)` 的指纹——同末日但不同内容的快照可区分。
   - `checkpoint.resume_source`：resume 时记录来源路径、来源工件的 run_id/state_checksum/last_completed（可定位续跑链）。
   - `output_checksum`：最终输出的指纹（metrics/交易/诊断/数据覆盖），同配置同数据重跑逐位一致；中途 checkpoint 不含（输出未完成）。
4. **性能（附带）**：universe 静态摘要（含 status SQL）只在运行开始算一次，`build_manifest` 动态拼 sizes；score 分位按样本版本缓存（长度未变复用排序）。实测 7 个月 × 5 票、checkpoint_every=1 每日保存：**universe status SQL 调用 1 次**（修复前每次保存 1 次）。

复验结果（§4.6）：全量 `pytest -q` 为 **285 passed, 2 warnings**（新增 3 个第四轮反例回归），ruff 全过；真实回测数字四轮一致。

## 4.6 第四轮修复后真实复验（2026-08-06）

> 本节记录第四轮修复后的当时结论；第五轮独立验收发现最终磁盘 checkpoint、数据快照身份和性能门禁仍未闭环，原“通过”结论已由 §4.7 撤销，后续实施与验收以 §4.8 为准。

`dividend_low_vol_v2`，沪深300 历史点时成分池，请求 2021-01-01 → 2026-08-05（实际数据到 2026-08-04，引擎截断），预热 2018-01-01，固定观察期 271 日，从头重跑；v1-core 带真实 checkpoint 落盘（checkpoint_every=250）：

- `no_overlay_v1`：formal 1082 日、896 笔成交、总收益 +51.96%、年化 +10.24%、最大回撤 14.05%、Sharpe 0.82、超额 +53.04%；`data_coverage.truncated=True`、`effective_eval_end=2026-08-04`。
- `v1_core_v1`：formal 1082 日、1753 笔成交、总收益 +28.26%、年化 +5.97%、最大回撤 11.73%、Sharpe 0.67、超额 +29.34%；真实 checkpoint `/tmp/v2_verify_ckpt.json` 的 manifest 含 git commit/dirty、data_snapshot_id、checkpoint.last_completed_date=保存日、resume_source 链（首次运行为 None），state/manifest 日期一致；但第五轮复核确认 **磁盘 checkpoint 不含 `output_checksum`**，该字段只存在于函数返回的最终 manifest 和结果摘要中。

**当时验收结论（已撤销）**：曾判断第四轮审查的 3 个阻塞与性能风险均已修复；第五轮独立复核证明 §14 可复现工件与性能风险只被部分修复，因此本节不再作为通过依据。

## 4.7 第五轮独立验收结论（2026-08-06）

第五轮不采信新增测试和状态文件的预设结论，重新执行第四轮原始反例、全量回归并直接解析真实磁盘工件。结论为：**仍未通过工程正确性验收**；raw callable 运行时绑定与中途 checkpoint 日期两项已经修复，剩余阻塞集中在 §14 可复现工件，性能风险也只部分改善。

### 4.7.1 已确认修复

1. **构造后替换 raw callable 会被拒绝**：配置构造完成后替换 `cfg.raw_computers[metric]`，即使新函数继续返回原声明的 `raw_fingerprint`，`run_v2_backtest()` 入口仍会重新计算函数绑定并硬失败。
2. **中途 checkpoint 日期真实**：10 日合成运行、`checkpoint_every=1` 下共捕获 10 个工件，所有 `state.last_completed_date` 都与 `manifest.checkpoint.last_completed_date` 相同；首日均为 2024-01-02，不再提前宣称运行到终点。
3. **正向回归稳定**：全量 `pytest -q` 为 `285 passed, 2 warnings`（94.67s），`ruff check stockfu tests main.py` 与 `git diff --check` 均通过；真实两组收益、成交笔数与此前一致。

### 4.7.2 仍然阻塞

1. **最终磁盘 checkpoint 没有 `output_checksum`**：循环内最后一次 `save_checkpoint(t)` 使用非 final manifest；循环结束后才计算 metrics 和 `build_manifest(..., final=True)`，但该最终 manifest 只返回到 `V2Result`，没有回写 checkpoint。真实 `/tmp/v2_verify_ckpt.json`（76,258,208 字节）不含 `output_checksum`，而 `/tmp/v2_reverify_result.json` 摘要含该字段。现有 `test_manifest_reproducibility_fields` 只断言返回对象，没有断言最终磁盘文件。
2. **`data_snapshot_id` 不是内容快照身份**：实现只哈希 `quote_snapshot`、`index_quote_daily`、`etf_quote_daily` 的 `(max(quote_date), count(*))`。隔离 SQLite 反例把行情值从 1 修改为 999、保持最大日期与行数不变，修改前后 ID 完全相同；分红、股票基础信息、历史指数成分等实际输入表也没有纳入。因此相同 ID 不能证明输入数据相同，更不能据此复现结果。
3. **dirty 源码不可还原**：真实工件为 `git.dirty=True`，只保存 commit 和 dirty 布尔值，没有保存 diff、未跟踪文件内容或可恢复 source bundle。当前 V2 还有大量未跟踪代码与 YAML，commit 不能代表实际运行代码。

### 4.7.3 性能风险仍在

universe 静态摘要已经做到每次运行只查询一次，这是有效改善；但 score 诊断缓存以样本长度为版本，而观察/正式期每天都会追加样本，因此默认每日 checkpoint 仍会每天重新执行完整 `_score_diagnostics()` 排序。10 日合成运行实测 10 次 checkpoint 写入、10 次完整诊断重算。真实 checkpoint 约 73 MiB，默认 `checkpoint_every=1` 仍会每日重写不断增长的完整 JSON；此前只验证 status SQL 次数，不能证明整体性能风险已消除。

## 4.8 一次性整改方案与验收门禁

本节是第五轮之后的实施准绳。不得再用“返回对象有字段”替代“磁盘工件有字段”，不得用 `(max_date, row_count)` 冒充内容快照，也不得在 canonical 工件为 dirty 时宣称确定性复现。修改范围应集中在 V2 artifact/checkpoint、数据快照接线、CLI 和对应测试，不需要改动已经通过验收的评分、交易与风险语义。

### 4.8.1 最终 checkpoint 两阶段落盘

当前 `save_checkpoint()` 同时拼 state、manifest 和 payload，使中途状态与最终输出生命周期混在一起。应拆成三个职责单一的函数：

```python
def build_checkpoint_state(last_completed: date) -> dict:
    ...

def build_checkpoint_payload(state: dict, manifest: dict) -> dict:
    ...

def persist_checkpoint(path: str, state: dict, manifest: dict) -> None:
    ...
```

运行期间继续原子保存可恢复的 partial checkpoint，但明确标记：

```json
{
  "checkpoint": {
    "finalized": false,
    "last_completed_date": "2024-01-02"
  },
  "output_checksum": null
}
```

循环结束后必须按以下顺序完成第二次原子覆盖：

1. 计算 formal equity、benchmark、metrics、observation/formal summary。
2. 使用实际完成日构造最终 checkpoint state 并计算 `state_checksum`。
3. 计算各输出组件 checksum 与总 `output_checksum`。
4. 构造 `checkpoint.finalized=True` 的最终 manifest，再计算 `run_id`。
5. 用最终 state、manifest 和 checksum 原子覆盖相同 checkpoint 路径。
6. `V2Result.manifest` 必须直接复用该最终 manifest；磁盘和返回对象不得分别构造。

实际完成日应维护为运行状态，而不是重新推断：

```python
actual_last_completed = resume_last_completed
for t in dates_all:
    ...
    actual_last_completed = t
```

这样可以覆盖正常运行、数据截断、空续跑以及从 partial/finalized 工件恢复等路径。保留最后一个 partial checkpoint 后再 final 覆盖的好处是：若 metrics 计算期间崩溃，仍有完整可恢复状态；恢复到相同终点时可以重新完成 finalize。

总输出 checksum 至少应绑定：

- 最终 `state_checksum`（间接覆盖 history、账户、订单、equity、trades、daily audit 等可恢复状态）；
- formal equity curve 与 benchmark curve；
- metrics；
- observation/formal summary；
- score diagnostics；
- 数据快照描述符。

manifest 可保存各组件 checksum 和一个总 checksum，不必再次复制完整数组。`run_id` 必须最后计算，覆盖 `output_checksum`、snapshot、源码身份和所有运行口径。

### 4.8.2 不可变数据快照

canonical 回测不应直接把持续变化的主库当成可复现快照。首选方案是用 SQLite backup API 生成只读快照，完成 WAL 合并后计算整个快照文件的 SHA-256，并生成 snapshot descriptor：

```json
{
  "snapshot_id": "sha256:...",
  "path": "data/snapshots/stockfu-20260804.db",
  "created_at": "2026-08-06T...+08:00",
  "data_end": "2026-08-04",
  "file_size": 123,
  "tables": {
    "quote_snapshot": {"rows": 1, "max_date": "2026-08-04"},
    "dividend_event": {"rows": 1},
    "stock_basic": {"rows": 1},
    "index_membership": {"rows": 1}
  }
}
```

`rows/max_date` 只作为人类审计摘要；真正的 `snapshot_id` 必须来自不可变快照文件内容。V2 vertical slice 使用的所有输入必须在同一快照或同一描述符中明确列出，至少包括行情、指数行情、ETF 行情、分红、股票基础/存续信息、历史指数成分和交易日历；新增 raw metric 时同步扩展依赖清单。

短期 resume 规则以正确性优先：把 `snapshot_id` 纳入 `checkpoint_identity`，快照不同直接拒绝恢复。若新数据到来，需要从头重跑。后续若必须支持跨快照延长终点，再增加 `parent_snapshot_id` 和旧截止日前缀内容 checksum；只有证明旧日期所有依赖表未变化时，才允许从父快照 checkpoint 续跑，不能默认把数据库更新视为纯追加。

### 4.8.3 源码身份与 canonical 门禁

正式工件必须来自干净、已提交的功能分支：

```python
if canonical and git_info["dirty"]:
    raise ValueError("canonical 回测要求干净工作树和已提交代码")
```

实施顺序应为：完成修改与测试 → 在普通功能分支提交 V2 代码和配置 → 确认工作树干净 → 生成数据快照 → 运行真实 canonical 回测。manifest 保存完整 40 位 commit、`dirty=False` 和依赖锁文件 hash。

探索性 dirty 运行可以保留，但必须标记 `reproducibility.status=non_canonical_dirty`，不得进入正式验收结论。若未来确实需要复现 dirty 运行，必须保存覆盖 tracked diff 与 untracked 文件的 source bundle 及 SHA-256；仅保存 dirty 布尔值不够。

### 4.8.4 Checkpoint 性能分两阶段治理

立即整改先消除已证实的重复工作：

1. 中途 checkpoint 不计算完整分位，只保存 `status=partial`、obs/formal 样本数和已有增量计数；完整 `_score_diagnostics()` 只在最终 finalize 时执行一次。
2. 保留 universe 静态摘要一次查询的现有修复。
3. manifest 不再重复嵌入完整 trades；使用 `n_trades + trades_checksum`，实际 trades 在 checkpoint state 或独立 artifact 中只保存一份。
4. `daily_audit` 拆为 append-only artifact，checkpoint 只保存 offset、条数与链式 checksum。
5. 在增量格式完成前，将 CLI 默认 `checkpoint_every` 调整为经实测可接受的 20、50 或 250，并同步修改“默认每天”的文档，不得继续让 73 MiB 完整 JSON 默认每日重写。

若产品要求每日都可恢复，则实现“周期性完整快照 + 每日 delta + checksum 链”：每 20–50 个交易日保存一个压缩完整 checkpoint，其余日期仅追加当天 history 变更、账户/订单/risk 状态、成交、净值和审计增量；恢复时加载最近完整快照并按链式 checksum 回放 delta。该方案完成前，不应宣称每日完整 checkpoint 的性能风险已经解决。

### 4.8.5 必须新增或修改的测试

最终磁盘工件测试：

1. 正常结束后读取磁盘，断言 `disk["manifest"] == result.manifest`。
2. 断言最终磁盘 `checkpoint.finalized=True` 且 `output_checksum` 非空。
3. 独立重算 `state_checksum`、各组件 checksum、总 `output_checksum` 和 `run_id`。
4. spy 每次落盘：除最后 finalize 外均为 partial；最后一次为 finalized。
5. 从 partial checkpoint 恢复，与不中断运行逐位一致。
6. 从 finalized checkpoint 延长时，`resume_source.source_run_id` 指向磁盘最终 run_id。

数据/源码身份测试：

1. 两个临时 SQLite 快照最大日期和行数相同、只修改 close，snapshot ID 必须不同。
2. 只修改一条 dividend 或历史指数成分，snapshot ID 必须不同。
3. 使用不同 snapshot ID 恢复 checkpoint 必须拒绝。
4. 相同源码、配置和不可变快照重复运行，最终 output checksum 必须相同。
5. canonical + dirty 工作树必须硬失败；探索性模式必须明确标记 non-canonical。

性能测试：

1. 10 日、`checkpoint_every=1` 下 `_score_diagnostics()` 最多执行一次。
2. universe status SQL 每次运行只执行一次。
3. 完整 checkpoint 写入次数符合配置间隔；若使用 delta，校验 full/delta 数量和 checksum 链。
4. 统计累计写入字节数，不能只检查最终文件大小。
5. 工件中不得同时在 state 和 manifest 重复保存完整 trades/daily audit。

单元测试必须使用临时数据库或注入的 snapshot descriptor，不得为了检查 metadata 每次扫描真实主库三张大表；真实主库只保留单独、显式执行的集成验收。

### 4.8.6 最终通过条件

只有以下项目全部满足，才能再次写“通过工程正确性验收”：

1. 全量 pytest、ruff、`git diff --check` 通过。
2. 构造后替换 callable 的原始反例被运行入口拒绝。
3. 所有中途 checkpoint 的 state/manifest 实际日期一致。
4. 最终磁盘 manifest 与 `V2Result.manifest` 完全一致。
5. 最终磁盘工件为 `finalized=True`，且 `output_checksum` 可独立重算。
6. 数据内容变化但日期/行数不变时，snapshot ID 必须变化。
7. checkpoint identity 绑定不可变 snapshot ID；不同快照不能静默续跑。
8. canonical 真实运行使用已提交、干净的 commit，工件为 `dirty=False`。
9. 默认 checkpoint 路径不再每日全量排序并重写重复的 73 MiB 数据。
10. 文档中的结论和字段清单必须从真实磁盘工件读取验证，不能从函数返回值推断。

完成上述门禁后，再从干净 commit 和不可变数据快照从头运行 `no_overlay_v1` 与 `v1_core_v1`，保存结果摘要、最终 checkpoint、snapshot descriptor 和独立校验报告；旧 `/tmp/v2_verify_ckpt.json` 只能作为第五轮失败证据，不得继续作为通过工件。

## 4.9 第五轮整改实施（2026-08-06）

按 §4.8 一次性整改方案逐项实施：

1. **§4.8.1 两阶段 checkpoint 落盘**：拆分 `build_checkpoint_state / build_checkpoint_payload / persist_checkpoint`；运行期间原子保存 partial 工件（`checkpoint.finalized=False`、`output_checksum=null`、`score_diagnostics` 只含 `status=partial` 与 obs/formal 样本数）；循环结束后 finalize：构造最终 state → 计算 `state_checksum` → 计算各组件 checksum（state/trades/formal_equity/benchmark/metrics/observation_summary/formal_summary/score_diagnostics/data_snapshot）与总 `output_checksum` → `finalized=True` 的最终 manifest（`run_id` 最后计算）→ 原子覆盖同一路径。`V2Result.manifest` 直接复用最终 manifest（磁盘与返回对象同一内容）。`actual_last_completed` 以 resume 起点为初值、逐日推进，覆盖正常/截断/空续跑路径。
2. **§4.8.2 不可变数据快照**：新增 `stockfu/backtest/snapshot.py`——SQLite backup API 生成只读快照到 `data/snapshots/stockfu-<sha256前12>.db`（WAL 合并后一致性副本），`snapshot_id = sha256(快照文件)`（内容身份，幂等去重复用）；descriptor 记录依赖表（quote_snapshot/index_quote_daily/etf_quote_daily/dividend_event/stock_basic/index_constituent）的 rows/max_date 审计摘要与 `data_end`/`file_size`/`calendar_source`；`validate_snapshot` 重算文件 hash 拒绝伪造/被改。`data_snapshot` 进 `V2RunConfig.manifest()` → **checkpoint identity 绑定不可变快照**，不同快照拒绝续跑。
3. **§4.8.3 源码身份门禁**：`V2RunConfig.canonical=True` 且 git dirty → 运行入口硬失败；manifest 记录 `reproducibility.status`（canonical / non_canonical_dirty / non_canonical）、40 位 commit、dirty 标志与依赖锁文件 hash（requirements.txt + pyproject.toml）。探索性 dirty 运行明确标记 non_canonical_dirty，不进入正式验收结论。
4. **§4.8.4 性能治理**：完整 `_score_diagnostics()` 只在 finalize 执行一次（partial 工件不排序）；manifest 不再嵌入完整 trades（`n_trades + trades_checksum`，完整列表只在 state 一份）；`daily_audit` 拆为 append-only artifact（`<checkpoint>.audit.jsonl`，链式 checksum + offset + 条数，checkpoint 只存摘要）；CLI 与 `v2_run.run()` 默认 `checkpoint_every` 从 1 调整为 20（73MiB 工件不再默认每日重写），文档同步。
5. **§4.8.5 测试**（新增 8 个）：快照内容变化（改值/改分红/改成份，日期行数不变）→ ID 必变；validate 拒绝丢失/篡改；快照进 identity（不同快照拒绝续跑，同快照可续跑）；canonical+dirty 硬失败与 non_canonical_dirty 标记；磁盘工件 == V2Result.manifest、finalized=True、state/output/run_id 独立重算、spy 落盘（中途全 partial、最后一次 finalized）；partial→finalize 两阶段；性能（诊断 1 次、写入 = 天数+1、trades/audit 不重复、audit 全在 artifact）。prefix invariance 测试注入 fake snapshot（§4.8.5：单元测试不备份 2.2GB 主库）。

**复验结果（§4.10）**：全量 `pytest -q` 为 **293 passed, 2 warnings**，ruff 全过。

## 4.10 第五轮整改后真实复验（2026-08-06）

`dividend_low_vol_v2`，沪深300 历史点时成分池，请求 2021-01-01 → 2026-08-05（实际数据到 2026-08-04，引擎截断），预热 2018-01-01，固定观察期 271 日，从头重跑（`canonical=False`，工作区尚有未提交改动；canonical 正式运行需在提交后执行，见 §4.11）：

- `no_overlay_v1`：formal 1082 日、896 笔成交、总收益 +51.96%、年化 +10.24%、最大回撤 14.05%、Sharpe 0.82、超额 +53.04%；`data_coverage.truncated=True`、`effective_eval_end=2026-08-04`。
- `v1_core_v1`：formal 1082 日、1753 笔成交、总收益 +28.26%、年化 +5.97%、最大回撤 11.73%、Sharpe 0.67、超额 +29.34%。
- 工件字段：`data_snapshot`（sha256 文件身份 + 依赖表摘要）、`reproducibility.status=non_canonical_dirty`、`checkpoint.finalized=True`、`output_checksum` 与 `component_checksums`（含 state）齐全、`run_id` 覆盖全部字段；真实 checkpoint 从磁盘读取验证（§4.8.6 门禁 10：从真实工件读字段，不从返回值推断）。

**验收结论**：§4.8.6 门禁 1–7、9–10 全部满足并有测试锁定；**门禁 8（canonical 干净 commit 真实运行）需要用户确认提交工作区改动后执行**。在此之前，探索性工件明确标记 non_canonical_dirty，不宣称确定性复现。

## 4.11 待用户确认事项

1. 提交 V2 代码与配置（含用户原有邮件/dividend/operator 改动需分开或一并处理）→ 干净工作树 → 执行 canonical 真实运行（`--canonical`），工件 `dirty=False`、`status=canonical`。
2. 完成 canonical 运行后，正式验收结论才可写「通过工程正确性验收」；旧 `/tmp/v2_verify_ckpt.json` 为失败证据工件，不作为通过工件。

## 4.12 第六轮验收阻塞修复（2026-08-07）

第六轮独立验收判定 §4.9–4.11 的实现「工件内部自洽但可复现性漏风」，提出 4 个阻塞。
本轮逐项修复，**全程未提交、未跑 canonical**；等回归 + 复验通过后再走 §4.11。

1. **阻塞① 快照未作真实数据源**：`run_v2_backtest` 此前只创建/校验 `cfg.snapshot`，
   实际取数（`_preload_market_range`/`_preload_dividend_events`/`_load_listing_and_industry`/
   `UniverseContext`/股票池解析）全部走全局 live `stockfu.db.engine`，工件绑定的 SHA 与
   实际数据源无关。修复：`db.py` 加 `_READ_ENGINE` ContextVar + `read_engine()`/
   `use_read_engine()`/`set_read_engine`/`reset_read_engine`/`has_read_engine_override`，
   `session_scope()`/`get_session()` 改走 `read_engine()`（未设→全局单例，全 app + V1 零变化；
   仅 V2 运行期设置，单线程 eval）；`snapshot.py` 加 `snapshot_engine`（`mode=ro`+`NullPool`，
   connect 只设 `busy_timeout`+`query_only`，**不设 WAL**——只读连接设 journal_mode 会
   `SQLITE_READONLY`）+ `descriptor_from_file`，`DEPENDENCY_TABLES` 追加 `security_master`；
   `run_v2_backtest` 拆 wrapper + `_run_v2_backtest_body`，wrapper `set_read_engine(
   snapshot_engine(cfg.snapshot))` 包住整个 body、finally 复位，两处直接 import 改
   `read_engine()`；**交易日历绑快照**（`_trade_calendar_days` 在 override 时跳过 akshare、
   从快照 `quote_snapshot` 派生——堵改主库/断网重跑日历漂移）；股票池解析在 `run()` 与
   `run_v2_backtest_cli` 都搬进 `use_read_engine`。
2. **阻塞② audit 恢复不验边界**：`_verify_audit_file`（重算 prev-hash 链 + 校验 offset/count，
   前缀篡改/缺失/截断硬失败，未提交尾部截断，n_days=0 清陈旧）；新运行清空输出 audit；
   resume 显式从 `<resume_from>.audit.jsonl` 读+校验，输出≠来源时重建。
3. **阻塞③ snapshot identity 不稳定**：`checkpoint_identity()` 只绑 `snapshot_id`
   （manifest 仍留完整 descriptor）；组件 checksum 同样只绑 `snapshot_id`；`resolve_snapshot`
   单一入口 resume 时从来源 checkpoint `manifest` 自动恢复 descriptor，文件缺失提示
   `--snapshot`、绝不静默重建。
4. **阻塞④ canonical 入口未接通且自触发 dirty**：补 CLI `--canonical`/`--snapshot`
   （全链透传）；`data/snapshots/` 入 `.gitignore`；canonical 门禁前移到写盘/预载之前；
   修 `--checkpoint-every` help（默认 20）。

**回归测试**（修复前失败、后通过）：`test_v2_audit_recovery.py`（audit 单元反例）、
`test_read_engine.py`（contextvar 机制）、`test_v2_snapshot.py` 增 `snapshot_engine` 只读/
缺失/重建 + 端到端隔离测试（合成代码 ZZTEST 仅在快照、主库无，证 listing/calendar/查询全读
快照）、`test_v2_engine_correctness.py` 增 audit 伪造截断/前缀篡改/缺失/清旧 + canonical+dirty
写盘前失败 + identity 跨重建稳定 + gitignore；合成/伪造 descriptor 测试补 stub
`snapshot_engine`→全局 engine 的 test seam。`create_data_snapshot` backup 失败时清 tmp（曾因
磁盘满残留 ~4G `.tmp-*` 孤儿）。

### 4.12.1 复验结果（2026-08-07）

全量 `pytest -q` 为 **316 passed, 2 warnings**，`ruff check stockfu/ main.py tests/`、
`git diff --check` 全过。`dividend_low_vol_v2` 沪深300 历史点时成分池、请求
2021-01-01→2026-08-05（实际数据到 2026-08-04 截断）、预热 2018-01-01、固定观察期 271 日，
从头重跑、全部取数走不可变快照（`data/snapshots/stockfu-2ee50075f50c.db`，运行后 sha256 仍
`2ee50075f50c`、-wal 0 字节，内容身份稳定）：

- `no_overlay_v1`：formal 1082 日、896 笔、总收益 +51.96%、年化 +10.24%、回撤 14.05%、
  Sharpe 0.82、超额 +53.04%。
- `v1_core_v1`：formal 1082 日、1753 笔、总收益 +28.26%、年化 +5.97%、回撤 11.73%、
  Sharpe 0.67、超额 +29.34%。

两组收益/成交/诊断（score n=324234、p01=6.65/p50=47.41/p99=86.41、coverage 0.9339、
mature 492960）**与 §4.10 逐项一致**——引擎重排零行为回归，且从不可变快照精确复现。

**结论**：§4.8.6 门禁 1–7、9–10 满足；门禁 8（canonical 干净 commit 真实运行）仍待用户
提交后执行。本轮反例回归锁定四个阻塞；canonical 运行前不得宣称确定性复现。另注：
`create_data_snapshot` 即便目标快照已存在也先 backup 到 tmp（~2GB），磁盘近满
（operator_cache ~22GB）时会 `database or disk is full`——用 `--snapshot <既有>` 复用可绕过；
属预存低效，后续可优化为「存在即跳过 backup」。

## 4.13 第七轮独立复验与整改方案（2026-08-07）

本轮不采信 §4.12 的预设结论，重新执行原始反例、全量回归并解析真实磁盘工件。
结论为：**第六轮的 audit 恢复、snapshot identity 稳定化、底层显式代码池取数和 CLI 参数
接线已经生效，但仍未通过工程正确性验收；不得先提交并运行 canonical。** §4.12 的“仅剩
门禁 8”结论由本节撤销。

### 4.13.1 已确认通过

1. 全量 `pytest -q` 为 **316 passed**；`ruff check stockfu main.py tests` 与
   `git diff --check` 均通过。
2. `_verify_audit_file` 能校验已提交前缀的条数、字节 offset 和链式 checksum，拒绝前缀
   篡改/缺失/截断，并截掉未提交尾部；真实 `/tmp/v2_verify_ckpt.json.audit.jsonl` 的
   1353 行、offset 和链式 checksum 可独立重算。
3. `checkpoint_identity()` 目前只绑定稳定的 `snapshot_id`；相同内容、不同 path/
   `created_at` 的 descriptor 身份相同，不同 `snapshot_id` 身份不同。
4. 底层 `run_v2_backtest(cfg)` 在显式 `cfg.codes` 路径会把日历、行情预载、分红、listing、
   UniverseContext 等查询切到 snapshot read engine；CLI 已有 `--snapshot`、`--canonical`，
   `--checkpoint-every` 帮助默认值为 20。
5. 旧真实磁盘 checkpoint 自身内部的 `state_checksum`、`output_checksum`、`run_id` 均可重算，
   快照文件 SHA-256 仍为 `2ee50075f50c767b75b3bda095b4ba321a74ce0801cdf88ade183309e8748cff`。

### 4.13.2 仍然阻塞

1. **默认候选池仍读 live 主库**。`v2_run.default_universe()` 在函数内直接
   `from stockfu.db import engine as db_engine`，所以外层 `use_read_engine(snapshot_engine)`
   对它无效。隔离反例让 live 库仅含 `600001`、snapshot 仅含 `000001`；上下文中
   `read_engine() is snapshot` 为真，但 `default_universe()` 仍返回 `['600001']`。
   CLI 省略 `--codes` 和公共 `run(..., codes=None)` 都走此错误路径，manifest 绑定的快照
   不能覆盖真实使用的候选池。
2. **canonical 门禁在公共入口仍晚于副作用**。底层 `run_v2_backtest()` 的门禁已前移，但
   `v2_run.run()` 会先 `resolve_snapshot()`，CLI 更会先 `init_db()`、解析/创建快照和候选池，
   最后才进入底层门禁。spy 反例在 dirty + `canonical=True` 下得到
   `SNAPSHOT_RESOLVER_CALLED_BEFORE_CANONICAL_GATE`。这与 CLI 帮助中的“不生成快照/不预载”
   承诺相反，也可能在最终拒绝前写主库 schema、创建约 2.1 GiB 快照。
3. **canonical provenance 可被错误“洗白”**。恢复 checkpoint 时只校验配置身份，不校验
   来源 manifest 的 reproducibility。实测先生成 `non_canonical_dirty` checkpoint（commit A），
   再模拟 clean commit B 以 `canonical=True` 恢复，运行被接受且最终 status 变成
   `canonical`。这样正式工件的一部分状态实际由 dirty/另一提交计算。另一个反例中
   `git_revision()` 返回 `{commit: None, dirty: None}`，当前门禁因只判断 dirty truthy 而放行，
   最终仍标记 `canonical` 且 `git_commit=None`；门禁不是 fail-closed。
4. **快照还不是运行期不可变文件**。`create_data_snapshot()` 生成的真实文件权限为 `0644`
   （当前进程同一 owner 可写）；`mode=ro/query_only` 只限制 V2 自己打开的连接，不阻止另一
   进程在回测期间原地修改或替换文件。引擎只在运行前校验 SHA，finalize 前不复验，因此可能
   读到混合内容后仍把运行前的 `snapshot_id` 写入正式工件。descriptor 还把实际已改为
   `quote_snapshot` 派生的日历误记为 `akshare_tool_trade_date_hist_sina`。
5. **当前代码缺少对应的真实最终工件证据**。`/tmp/v2_verify_ckpt.json` 的 mtime 为
   2026-08-06 23:22，而 `v2_engine.py` 的本轮修改晚于它。其磁盘 snapshot 组件 checksum
   `73346e...` 等于旧的完整 descriptor hash；按当前代码仅对 snapshot_id 重算应为
   `d9c245...`，二者不等。`/tmp/v2_v1core.log` 只有终端摘要，没有当前代码生成的 checkpoint
   可供独立校验。因此 §4.12 的真实工件不能计作当前版本的门禁 4/5/7/10 证据。

### 4.13.3 一次性修复方案

1. **统一候选池读取**：`default_universe()` 改用 `read_engine().connect()`；新增 live/snapshot
   内容相反的测试，分别覆盖函数、`run(codes=None)` 和 CLI 省略 `--codes`，断言候选代码、
   `codes_fingerprint` 都只来自 snapshot。继续 `rg` V2 传递闭包中的直接 `engine` 引用，只允许
   `snapshot.db_path()` 为创建快照而访问 live engine。
2. **统一 fail-closed 预检**：提取无副作用的 `canonical_preflight(canonical)`。当 canonical
   为真时必须同时满足 `commit` 为完整 40 位、`dirty is False`、依赖 hash 可取得，否则拒绝。
   `run_v2_backtest_cli()` 必须在 `init_db()` 之前调用，`v2_run.run()` 必须在
   `resolve_snapshot()` 之前调用，底层 `run_v2_backtest()` 保留同一门禁作纵深防御。spy 测试
   应断言 dirty/未知 Git 状态下 `init_db`、descriptor/hash、snapshot create、股票池查询、
   checkpoint 写入均为 0 次。
3. **锁定 canonical 恢复链**：canonical resume 读取来源 manifest 后，必须要求来源
   `reproducibility.status == canonical`、`git_dirty is False`、`git_commit == 当前 commit`、
   `deps_hash == 当前 deps_hash`；任一缺失或不等即拒绝。不得把 non-canonical checkpoint
   提升为 canonical。新增 dirty 来源、clean 但未声明 canonical、不同 commit、不同 deps 四个
   拒绝测试，以及同一 clean commit canonical partial 恢复通过测试。
4. **闭合快照不可变性**：新建或复用 target 后设只读权限（至少移除 owner/group/other 写位），
   snapshot 连接可加 SQLite `immutable=1`；更关键的是在最终 diagnostics/checkpoint finalize
   前再次 `validate_snapshot(cfg.snapshot)`。末次 SHA 不一致时硬失败且不得留下
   `finalized=True` 工件。descriptor 的 `calendar_source` 改为真实的
   `quote_snapshot.distinct_quote_date`。新增运行中修改/替换快照的反例测试。
5. **重新生成验收证据**：上述代码和测试完成后先跑全量 pytest/ruff/diff check；再在提交后的
   干净分支用同一个显式 `--snapshot` 从头跑 no-overlay 与 v1-core，分别保存最终 checkpoint、
   audit artifact、命令和日志。独立重算 snapshot SHA、audit 链、state/组件/output/run_id，
   并确认候选池 fingerprint、`git_commit`、`dirty=False`、`status=canonical`。只有这一步通过，
   才能满足 §4.8.6 门禁 8 并恢复“通过工程正确性验收”的结论。

### 4.13.4 必须补充的回归测试

- snapshot/live 候选池相反时，默认入口必须选择 snapshot；旧实现必须稳定失败。
- public `run()` 与 CLI 的 canonical dirty/未知 Git 预检发生在所有写入和快照解析之前。
- non-canonical/dirty/不同 commit/不同依赖的 checkpoint 不得被 canonical resume。
- snapshot 在运行中发生修改或路径替换时，finalize 必须拒绝。
- 当前代码生成的真实磁盘 manifest 与返回值相等，所有 checksum 按当前口径可独立重算。

**本轮验收结论**：316 个现有测试全绿不等于门禁完成；以上 4 个代码阻塞和 1 个真实证据阻塞
解决前，不提交并启动 canonical 正式运行，也不写“通过工程正确性验收”。

## 4.14 第八轮独立复验与 canonical 收尾方案（2026-08-07）

本轮重新执行 §4.13 的四个原始反例、定向测试、全量回归，并检查快照与真实工件时间线。
结论为：**§4.13.2 的四个代码阻塞已经修复；但 canonical 的依赖身份仍不是真正的锁文件，
且当前代码尚未生成真实最终工件，因此整体仍未通过 §4.8.6。**

### 4.14.1 四个原始阻塞复验结果

1. **默认候选池快照隔离通过**：live engine 仅含 `600001`、snapshot engine 仅含
   `000001` 时，`read_engine() is snapshot` 且 `default_universe()` 返回 `['000001']`；
   `default_universe()` 已改用 `read_engine().connect()`，公共 `run(codes=None)` 与 CLI 省略
   `--codes` 也有相反内容测试覆盖。
2. **canonical 门禁顺序通过**：dirty + `canonical=True` 的 public `run()` 直接抛错，
   `resolve_snapshot` 调用次数为 0；CLI 在 `init_db()` 之前预检。未知 Git、短 commit、依赖
   identity 缺失均 fail-closed。
3. **canonical resume provenance 通过**：独立重放 `non_canonical_dirty` 来源 → clean 另一
   commit 的提升反例，当前实现拒绝；clean 但 non-canonical、不同 commit、不同 deps 也拒绝，
   同一 clean commit/deps 的 canonical 来源允许恢复。
4. **运行期快照校验通过**：新建/幂等复用的内部快照移除写位，descriptor 日历来源改为
   `quote_snapshot.distinct_quote_date`；finalize 写最终 manifest/checkpoint 前再次重算快照 SHA，
   失败时最多保留 partial 工件，不留下 `finalized=True`。
5. 定向回归为 **64 passed**；全量 `pytest -q` 为 **333 passed**；
   `ruff check stockfu main.py tests` 与 `git diff --check` 通过。

### 4.14.2 剩余阻塞一：`deps_hash` 哈希的不是依赖锁

当前 `deps_hash()` 把 `requirements.txt + pyproject.toml` 的文本作为“依赖锁身份”。但项目
`requirements.txt` 的 17 个有效依赖项全部是 `fastapi`、`sqlalchemy`、`akshare` 等**无版本
约束的浮动名称**，`pyproject.toml` 也明确说明仅用于 ruff，不包含项目依赖。实测：

```text
requirements_entries=17 pinned_or_direct=0 floating=17
deps_hash=a8f75d740010e45e78cfd79cbd37222523fb07ef34747aff75d01497db2ebda8
```

相同文本 hash 在不同日期安装时可以解析为不同 direct/transitive 版本；因此 manifest 中的
`deps_hash` 不能复原实际运行环境，不满足 §4.8.3 的“依赖锁文件 hash”。当前
`canonical_preflight(True)` 会把这份浮动文件当成有效锁并放行，是 canonical 假阳性。

整改方案：

1. 将人工维护的浮动依赖声明保留为 `requirements.in`（或保留现文件但明确它不是 lock），用
   `pip-compile --generate-hashes`、uv 或等价工具生成并提交包含全部传递依赖、精确版本和包 hash
   的 `requirements.lock`；canonical 环境必须从该 lock 建立隔离 venv。
2. `deps_hash()` 只接受明确支持的锁文件（如 `requirements.lock` / `uv.lock`），不得再让未锁的
   `requirements.txt` 或仅含 ruff 配置的 `pyproject.toml` 满足门禁；manifest 同时记录
   `lock_file`、`lock_sha256`。
3. 建议再记录实际运行环境 identity：Python implementation/version、平台、SQLite 版本，以及
   规范化的已安装 distribution `name==version` 列表 hash。canonical preflight 校验当前环境与
   lock 一致；至少应执行 `pip check` 并拒绝缺包/版本漂移。
4. 新增反例：只有浮动 `requirements.txt` 时 canonical 拒绝；lock 任一版本/hash 改变时 identity
   改变；当前环境与 lock 不一致时拒绝；同一 lock/环境重复运行 identity 相同。

### 4.14.3 剩余阻塞二：当前代码没有真实最终工件

当前 V2 三个核心文件的修改时间约为 2026-08-07 09:58–10:00；现有真实 checkpoint
`/tmp/v2_verify_ckpt.json` 生成于 2026-08-06 23:22，`/tmp/v2_v1core.log` 生成于 01:19，均早于
当前代码，且没有更新后的最终 checkpoint/audit 可解析。真实快照 SHA 仍正确：

```text
2ee50075f50c767b75b3bda095b4ba321a74ce0801cdf88ade183309e8748cff
```

该既有快照文件创建于只读权限修复前，当前模式仍为 `0644`。finalize 二次 SHA 已能阻止把正常
并发修改接受为正式工件；正式 canonical 前仍应将该内容寻址文件去掉写位，或由修复后的
`create_data_snapshot` 幂等复用并加固，再确认 SHA 不变。

最终收尾顺序：

1. 生成、审阅并提交真实依赖 lock，完成上述门禁测试；全量 pytest/ruff/diff check 通过。
2. 分离或确认工作树中的邮件/dividend/operator 改动，提交 V2 代码、配置、测试、文档和 lock；
   确认 branch clean，记录完整 commit。
3. 对显式快照复验 SHA 并移除写位；从头运行 no-overlay 与 v1-core 两组 `--canonical`，分别指定
   checkpoint 路径并保存命令和完整日志，不复用旧 non-canonical checkpoint。
4. 从新磁盘工件独立重算 snapshot SHA、audit count/offset/链、state checksum、全部组件
   checksum、output checksum 和 run_id；确认 `status=canonical`、`dirty=False`、commit/lock/
   environment identity 正确，候选池 fingerprint 与快照查询结果相符。
5. 两组均通过后，才把 §4.8.6 门禁 8 和 10 标记完成，并恢复“通过工程正确性验收”的结论。

**本轮验收结论**：第七轮四项代码整改验收通过；项目整体暂不通过。先补真正的依赖锁及其
门禁测试，再提交和运行 canonical，不能用当前浮动 requirements 的 hash 作为正式证据。

---

## 4.15 第九轮实施：真实依赖锁 + 环境身份（2026-08-07）

按 §4.14.2 整改方案实施完成，全量 `pytest -q` = **338 passed**（含 §4.14.2 方案 4 的新增反例），
ruff 与 `git diff --check` 通过。

### 4.15.1 已落地

1. **真实依赖锁**：`uv pip compile requirements.txt -o requirements.lock --generate-hashes`
   生成 73 个包（全部传递依赖、精确版本、sha256 hash）；`requirements.txt` 顶部注明其为
   浮动声明而非锁。`requirements.lock` 不在 .gitignore 内，随代码提交。
2. **门禁只认锁**：`deps_hash()` 替换为 `lock_identity()`——只接受 `requirements.lock`/
   `uv.lock`；浮动 `requirements.txt`/`pyproject.toml` 不再满足 canonical（原假阳性已封死）。
3. **环境一致性**：`lock_matches_environment()` 逐包校验 lock 版本 == 已安装版本（缺包/漂移
   即拒绝）；`environment_identity()` 记录 python 实现/版本、平台、SQLite 版本与规范化
   已安装 distribution 列表 hash。manifest `reproducibility` 新增 `lock_file`、`env_identity`，
   `deps_hash` 字段保留（语义=锁文件 sha256，resume 链校验复用）。
4. **新反例测试**：无锁文件拒绝；lock 内容变 identity 变；环境与 lock 不一致拒绝；
   同环境重复 identity 相同；uv hash 格式解析；缺包/漂移检测。

### 4.15.2 环境已同步（生成/测试统一依赖）

确认方案：**生成与测试环境使用同一套 uv 固化依赖**。已执行
`UV_BREAK_SYSTEM_PACKAGES=1 uv pip install --system -r requirements.lock`
（PyPI 直连慢，经 /opt/clash/proxy.sh 代理完成），环境从 18 漂移 + 12 缺包同步为
与 lock 完全一致：`lock_matches_environment(lock) is True`（akshare 1.18.82、
pandas 3.0.5、yfinance 1.5.2 等 73 包全对位）。playwright 1.61→1.62 后浏览器二进制
不匹配，已 `playwright install chromium` 重装并验证可启动。升级后全量 `pytest -q`
= **338 passed**，ruff / `git diff --check` 通过——测试与生成环境在锁版本下均健康。

### 4.15.3 待办（§4.14.3 不变）

提交干净分支（含 requirements.lock）→ 环境同步/隔离 → 显式快照重跑两组 canonical →
独立校验新工件。只有这些完成，才恢复“通过工程正确性验收”结论。

## 4.16 第十轮独立复验：单一生产依赖口径（2026-08-07）

本轮按用户确认的原则验收：生产与验证共用**同一套固定版本依赖**，不要求拆分 dev/prod 多套
锁。结论为：**当前 `requirements.lock` 本身和磁盘 Python 环境已经对齐；但门禁仍有一个
空锁/`uv.lock` fail-open，正在运行的生产进程尚未加载新环境，部署文档也仍指向浮动依赖。**

### 4.16.1 已确认通过

1. `requirements.lock` 由 uv compile 生成，含 73 个直接/传递包，全部使用精确版本并附下载
   SHA-256；当前 lock SHA-256 为
   `ef8036cd7a8ef7ff03610bf5381a6646ded29dca69be2716e8eca6ec3ad04ab7`。
2. 当前验证解释器为 `/usr/bin/python3`（CPython 3.12.3）；`lock_matches_environment()` 返回
   true，`uv pip check`/`pip check` 无破损依赖。带实际同步参数执行 uv dry-run：

   ```text
   Using Python 3.12.3 environment at: /usr
   Checked 73 packages
   Would make no changes
   ```

3. 独立漂移反例把 `lock_matches_environment` 置为 false 后，canonical preflight 正确拒绝；
   clean commit + 当前 lock/环境可通过预检并写入 lock/env identity。
4. 全量 `pytest -q` 为 **338 passed**；`ruff check stockfu main.py tests` 与
   `git diff --check` 通过。

### 4.16.2 剩余代码阻塞：锁解析可空集放行

`_SUPPORTED_LOCK_FILES` 同时声明支持 `requirements.lock` 和 `uv.lock`，但
`_parse_lock_versions()` 只解析 pip requirements 风格的 `name==version`，不解析标准 TOML
`uv.lock`。隔离反例只放一个包含 `fastapi=0.1.0` 的 TOML `uv.lock`，实际结果为：

```text
lock_identity = uv.lock
parsed = {}
lock_matches_environment = True
```

空的或格式损坏的 `requirements.lock` 也有同样问题：循环零次后返回 true。因而 canonical 门禁
仍不是严格 fail-closed。按“只要一套固定依赖”的用户口径，最小修复是：

1. 当前阶段只支持并只查找 `requirements.lock`，移除未实现的 `uv.lock` 分支；以后若切换格式，
   应替换解析器和测试，而不是同时声明两种真源。
2. 解析结果为空必须返回 false/抛错；锁中每个有效顶层 requirement 必须是精确 `==`，且至少有
   一个 `--hash=sha256:`。无法识别、重复冲突或缺 hash 时 canonical 拒绝。
3. 新增 TOML `uv.lock` 不得误通过、空锁不得通过、无 hash/非精确版本不得通过四个反例；保留
   当前真实 lock 73 包与环境匹配的正向测试。

### 4.16.3 生产运行态尚未切换

宿主全局进程表显示当前 Web 生产进程为 `python3 main.py --serve`，解释器路径确实是
`/usr/bin/python3.12`，与验证解释器相同；但它启动于 **2026-08-04 19:33:47**，依赖同步发生在
2026-08-07。同步前快照与 lock 至少存在以下差异：

| 包 | 生产进程启动时磁盘版本 | 当前 lock/验证版本 |
|---|---:|---:|
| fastapi | 0.139.0 | 0.141.1 |
| uvicorn | 0.51.0 | 0.52.1 |
| akshare | 1.18.77 | 1.18.82 |
| pandas | 3.0.3 | 3.0.5 |
| playwright | 1.61.0 | 1.62.0 |

Python 已导入模块不会因磁盘包升级自动替换，且延迟 import 可能形成新旧混合进程；所以“磁盘
环境与 lock 一致”不等于“当前生产进程已一致”。需要在修复并提交后按现有运维方式重启服务，
确认新 PID/启动时间、`/proc/<pid>/exe=/usr/bin/python3.12`、HTTP 健康检查和关键邮件/浏览器
smoke 均通过。此动作会中断线上进程，本轮只读验收未代用户执行。

### 4.16.4 部署文档必须统一到唯一 lock

`README.md` 快速开始仍写 `pip install -r requirements.txt`，`pyproject.toml` 仍注释“包管理走
裸 pip + requirements.txt”，而 `requirements.txt` 首行也仍展示浮动安装命令。新部署照此执行
会绕过 lock，与用户要求相反。应统一为生产和验证都执行同一命令，例如：

```bash
UV_BREAK_SYSTEM_PACKAGES=1 uv pip install --system -r requirements.lock
```

浮动 `requirements.txt` 只作为更新 lock 的输入，并明确不得用于部署/测试；README、工作区说明
和实际启动流程都应指向 `requirements.lock`。若将来改用隔离 venv，也必须让生产与验证同时切换，
不能形成两套环境。

### 4.16.5 最终结论

真实固定 lock 与当前磁盘环境这一项已通过；项目整体仍暂不通过。完成空锁 fail-closed、统一部署
文档、重启生产进程并 smoke 后，才能确认“实际生产环境 = 验证环境”。之后仍需提交 clean commit
并生成两组当前代码的 canonical checkpoint/audit 工件，才能完成 §4.8.6 最终门禁。

### 4.16.6 空锁 fail-closed 与部署文档已落地（2026-08-07，待用户审核提交）

1. `_SUPPORTED_LOCK_FILES` 只保留 `requirements.lock`，移除未实现的 TOML `uv.lock`；
   `_parse_lock_versions()` 改为 fail-closed：空锁、非精确版本（`>=`/`~=`/多版本）、缺
   `--hash=sha256:<64hex>`（非 sha256 哈希同样拒绝）、无法识别的非注释行、同包重复声明
   一律 ValueError；`lock_matches_environment()` 对解析结果为空再兜底返回 False。
2. 新增 5 个反例测试：TOML uv.lock 不得误通过（`lock_identity()` 只认
   requirements.lock → None）、空锁不得通过、无 hash/非 sha256 不得通过、非精确版本不得
   通过、重复声明不得通过；既有 drift 测试改用带合法 hash 的锁文件。
3. 隔离实测：TOML uv.lock → `lock_identity() = None`；TOML 内容塞进 requirements.lock、
   空锁（仅注释）、无 hash、`fastapi>=0.141.1` 全部 ValueError 拒绝；真实
   requirements.lock 解析 73 包且 `lock_matches_environment() = True`。
4. 部署文档统一到唯一 lock：README 快速开始、pyproject 注释、requirements.txt 首部均改为
   `UV_BREAK_SYSTEM_PACKAGES=1 uv pip install --system -r requirements.lock`（README 附
   `playwright install chromium`）；浮动 requirements.txt 明确仅作更新 lock 的输入，
   不得用于部署/测试。
5. 全量 `pytest -q` = **343 passed**（338 + 5 新增）；ruff / `git diff --check` 通过。
   本轮未提交、未重启生产（§4.16.3 仍待办）。

## 4.17 第十一轮修复：canonical 边界继续 fail-closed（2026-08-07）

独立反例发现并修复三个不影响当前从头业务计算、但会破坏 canonical 来源证明的边界：

1. `git rev-parse` 成功但 `git status --porcelain` 失败/超时时，`git_revision()` 原先把
   unknown 折叠成 `dirty=False`；现保留为 `dirty=None`，由 preflight 明确拒绝。
2. lock parser 原先会把 requirement 前的缩进孤立 hash 错绑给后续包，并只做小写转换，导致
   `typing-extensions` / `typing_extensions` 可绕过重复声明检查；现拒绝没有所属 requirement
   的续行，并按 PEP 503 将连续 `-`/`_`/`.` 归一为 `-` 后再判重。
3. canonical resume 原先只比较 status/commit/deps hash，同一 lock 下可把环境 A 的 checkpoint
   在环境 B 续跑后仍标 canonical；现要求来源与当前完整 `env_identity`（Python、平台、SQLite、
   已安装 distribution 集合 hash）逐项一致，缺失或变化均拒绝。

新增 5 个反例/正向测试；旧实现下 4 个稳定失败、修复后相关定向测试 8/8 通过。全量
`pytest -q` = **348 passed**，ruff 与 `git diff --check` 通过；真实 lock SHA 仍为
`ef8036cd7a8ef7ff03610bf5381a6646ded29dca69be2716e8eca6ec3ad04ab7`，解析 73 包且与环境匹配。
既有快照 `stockfu-2ee50075f50c.db` 已从 0644 加固为 0444，内容 SHA 不变。本轮仍未提交、未重启
生产、未生成两组当前代码的 canonical 工件；这些运维门禁完成前不恢复“工程正确性验收通过”。

## 4.18 clean commit canonical 最终收尾（2026-08-07）

代码与混合工作树已按逻辑提交：`00ef26c`（V2 引擎/配置/lock/测试/文档）、`0ae6330`
（operator cache 幂等修复）、`98be076`（signal mail 长表）；随后工作树为空。canonical preflight
确认 `git_commit=98be076c2d2bcf1efc25d961dbe1b4d2608eafb7`、`git_dirty=false`、lock SHA
`ef8036cd7a8ef7ff03610bf5381a6646ded29dca69be2716e8eca6ec3ad04ab7`、当前环境与 73 包锁一致。

从同一只读快照 `sha256:2ee50075f50c767b75b3bda095b4ba321a74ce0801cdf88ade183309e8748cff`
并行从头运行两组 canonical，均 exit 0、formal 1082 日，结果与修复后的非 canonical 复验逐项一致：

- `no_overlay_v1`：run_id `7b7c9fc1cccbb88a6e797668d3ae5399a685c71c788cf76cc5da8c70402332a6`，
  896 笔，总收益 51.96%，年化 10.24%，最大回撤 14.05%，Sharpe 0.82，超额 53.04%。
- `v1_core_v1`：run_id `1741d925d3adaf8975ef6a666bbb92f5bdcaa43aef9e1a59ebaacf4e5e7c737d`，
  1753 笔，总收益 28.26%，年化 5.97%，最大回撤 11.73%，Sharpe 0.67，超额 29.34%。

独立脚本只读磁盘工件重新构造 formal equity/benchmark/metrics/raw summary/score diagnostics，并重算
state、trades、全部组件、output checksum、run_id；同时逐行重放 1353 行 audit checksum 链、复算
offset/文件大小、2.1 GiB 快照 SHA/只读位以及快照中 939 只沪深300历史候选 fingerprint。两组 14 类
检查全部为 true，报告为 `data/backtest/v2-canonical-verification-98be076.json`。

至此 §4.8.6 的 10 项门禁全部满足，恢复结论：**V2 当前 vertical slice 通过工程正确性验收，可用于
可复现研究回测。** 生产 Web 进程仍需单独部署重启与 HTTP/邮件/浏览器 smoke；这属于部署验收，
不改变本次离线回测引擎 canonical 结论。

## 5. V2 策略评分邮件能力（2026-08-10）

§0.1 曾把「实时荐股/邮件/API 迁移」列为暂不做项。本节落地其中的**信号邮件**部分：把 V1
`signal_scan → signal_mail` 管线在 V2 十策略上重做。完整规格见
`docs/SPECS/signal-recommendation-mail.md` 末尾「V2 十策略评分邮件」节；要点留痕：

- **粒度方案①**：V2 策略分天生 0–100（profile 映射→alpha 加权，契约「禁止再映射」），**不复用 V1
  的 `score_full` 线性映射**——故 §0.1 时代 V1 那套「评分刻度/仓位刻度耦合」TODO 在 V2 不存在。
  跨策略分布差异（绝对锚点 vs 历史 ECDF 混合）通过邮件图例的校准元数据（P05/中位/P95/饱和/可交易）
  显式暴露，按列读，不做横向再映射。
- **单日评分 A2**：引擎只有 `run_v2_backtest` 循环入口，故新增 `stockfu/services/v2_signal.py
  ::V2SignalScorer.score(as_of)`，复用 `HistoryState`/`FactorScorer`/`AlphaAggregator`/raw_computers/
  `_preload_market_range`/`_backtest_series_ctx`，跑「只评分+历史、无交易/账户/风控」的最小循环。
  非采样日只推进 `history.cutoff`；必须挂 `_backtest_series_ctx` 否则 valuation 类算子查库（~108ms/次）。
- **渲染发信**：`stockfu/services/signal_mail_v2.py` + CLI `main.py --v2-signal-mail`。复用 `services.mail
  .send_card_email`；Playwright 进程内 `set_content` 出图（无 web 路由依赖）。
- **验证**：`--v2-signal-mail` 真发信成功（sent:true，3 页/800 股/10 策略）；图片经视觉核验无误；
  ruff 全绿。as_of 超 DB 数据末日时截断到 `max(sctx.dates)`（踩过交易日历预埋未来日的坑，已修）。
- **未做（后续）**：评分未持久化（无 V2 版 `signal_scan_run`，每次内存一次性）、未接 `--schedule`
  定时、无逐股订阅模型。本能力为研究阶段产物，非实盘信号。
