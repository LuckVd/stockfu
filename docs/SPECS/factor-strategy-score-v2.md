# 因子、策略评分与回测系统 V2 设计

状态：设计稿，禁止据此阶段直接修改业务代码

日期：2026-08-06

适用范围：因子原始值、0–100 评分、策略聚合、横截面选股、历史状态、回测、实时荐股及审计输出

## 1. 结论先行

本次不是修复邮件展示，而是替换整个评分与回测链路的量纲契约。最终系统必须满足：同一个策略对同一只股票在每个交易日产生一个 0–100 的策略评分；这个分数在同一天不同股票之间可以比较，也能与该股票过去的分数比较；同一因子在不同策略中使用时含义完全相同。

确定采用以下方案：

1. 每个因子先产生不带主观量纲的原始值，再由独立、带版本的评分档案映射到 0–100；不再让各算子自行输出任意范围的 score 后直接相加。
2. 有自然中点的因子采用固定锚点等比例映射。例如收益率 0、Beta 1、布林位置 0.5、量比 1 都映射为 50。锚点之间严格线性插值，范围外截断到 0 或 100。
3. 没有稳定自然上下限、或者绝对值会随市场结构变化的因子，采用“绝对锚点 + 全市场历史 + 行业历史 + 个股自身历史”的可配置混合映射。股息率、盈利收益率、账面市值比、规模、波动率、换手率属于这一类。
4. 历史分位是过去样本上的经验位置，不是当日横截面排名。t 日所有股票使用同一份截至 t-1 日的参考状态评分，所以不会为了每天平均分布而强行制造赢家和输家。
5. 每次正式全周期回测以前 1/5 交易日作为初始观察期，不交易；后 4/5 是正式评价期。后 4/5 的新数据仍持续进入 expanding 或 rolling 历史状态。冻结的是公式、窗口、权重和版本，不是历史分布。
6. t 日评分只能读取 t-1 及以前的历史状态。先计算 t 日原始值和评分、生成最早在 t+1 执行的订单，再把 t 日观测写入历史状态。
7. 历史最高、最低、样本数和分位锚点需要逐期记录，作用是审计、复现和监控，不能直接作为每日动态的 0/100 端点。
8. 所有旧策略先做完整、可机器读取的复现归档。归档通过门禁后，52 份 V1 源配置全部退出运行目录并删除；不保留长期兼容层。新系统只重建经过选择的 alpha 定义和组合/风险政策。
9. 不是全部代码推倒重来。可靠的点时数据、纯原始指标算法、成交记账、费用模型、交易可行性和风险机械规则可以在测试通过后复用；V1 评分公式、weighted_sum 量纲、score_full 仓位映射、扫描服务二次映射和依赖逐股票调用的回测编排必须重写。

## 2. 用户目标的精确定义

### 2.1 策略分数回答的问题

策略分数 S(strategy, asset, t) 的问题是：“在策略定义不变、只使用当时可见信息的前提下，这只股票在 t 日对该策略有多合适？”

- 0：有明确而极强的反对证据。
- 50：证据中性，或者有效正负证据大体抵消。
- 100：有明确而极强的支持证据。
- 分数越高永远越符合该策略，不允许某些因子高分看多、另一些因子高分看空。
- 50 不是当日中位股票，也不保证每天一半股票高于 50。
- 分数不是目标仓位。评分、选股、仓位和风险控制必须是四个独立层次。

### 2.2 两种可比性

横向可比要求同一天所有合格股票使用相同的因子档案、历史截止日、市场池定义和映射版本。纵向可比要求同一股票不同日期的 50、70、90 保持同一语义，历史窗口演进可解释且不回写过去。

历史经验分数表达“相对当时可见历史的稀有程度”。因此 2010 年的 80 和 2026 年的 80 语义相同，都是在各自时点的既定参考系里具有较强证据；它们不声称原始值相等。需要比较原始经济量时必须同时查看 raw_value。

### 2.3 非目标

- 不追求让每日分数均匀分布。
- 不把 0–100 当收益率预测概率，除非未来单独完成概率校准。
- 不在本阶段挑选最终获利策略或调优因子权重。
- 不用回测全样本最终最高/最低值反算历史分数。
- 不为了兼容旧结果而保留两套评分真相。

## 3. 当前系统的问题与替换边界

### 3.1 已核对的事实

- 当前共有 33 个因子算子和 52 份策略 YAML。
- 52 份 YAML 是源文件全集，不等于 52 个当前运行策略：seed._STRATEGIES 当前选择 29 个基础文件，_RETAINED_STRATEGY_IDS 展开后保留 31 个运行 id；其他 YAML 已不在 seed 运行清单中，但仍需归档其研究意图和配置。
- 各算子的分数范围包括无界、正负 20、正负 15、仅 0–20、仅 -20–0 和离散档位；0 同时被用作缺失、中性和无信号。
- weighted_sum 直接相加这些不同量纲，结果不是稳定的策略强度。
- score_full 通常为 8 或 10，随后把聚合原始分线性映射仓位；实际样本中大量策略分数被截断到展示层的 0 或 100。
- cap_and_rank 和 top_n_picker 在聚合后才做横截面竞争，无法补救聚合前已经失真的因子量纲。
- value 被 26 个策略引用，但当前主要使用单只股票 PE 的五年自身历史分位，PB 结果被丢弃；亏损 PE 变成缺失。
- graham_value 已把 PE、PB 和股息混成一个因子，外层策略又经常叠加 dividend_yield，导致重复暴露。
- 15 份红利策略使用完全相同的 dividend_yield 1.0、low_volatility 0.8、value 0.6，仅风险和止盈规则不同。多组动量、反转和布林配置也有同样重复。
- operator_result 同时缓存原始语义和旧分数。历史映射依赖全局状态后，不能继续假设它是单只股票的纯函数。

### 3.2 必须替换

以下对象不允许继续作为 V2 的业务契约：

- BaseOperator/OpResult 中由因子自行决定 score 量纲的约定。
- weighted_sum 对旧 score 的直接求和。
- score_full、dead 对旧聚合分的仓位换算。
- signal_scan 中 50 + raw/score_full × 50 的二次映射。
- 把缺失值返回 score=0 的约定。
- 一个 per-code analyze 调用同时完成原始值、映射、聚合和仓位的编排。
- value 和 graham_value 的当前复合口径。
- 将 alpha、rebalancer、止盈、止损、组合刹车混在同一策略身份中的配置结构。

### 3.3 可以有条件复用

以下部分只有在独立测试证明点时正确、输入输出纯净后才能复用：

- quote_snapshot、估值、分红、历史指数成分、证券上市退市信息等点时数据。
- 只计算原始数学量的函数，例如 RSI、收益率、标准差、回归、布林带、MACD 和 ATR。
- 除权除息口径、不复权股息率分母、动态股票池和交易可行性检查。
- 现金、持仓、费用、滑点、涨跌停、停牌、成交和净值记账。
- 止盈、止损、总敞口、回撤刹车等不读取未来信息的机械规则。

复用标准不是“文件还在”，而是：职责单一、点时正确、无旧 score 依赖、无隐藏全局状态、可由测试独立验证。否则删除后按 V2 契约重写。

## 4. V2 分层架构

每日数据流固定为：

    PendingOrders(from <t) -> ExecutionSimulator(t) -> CashAndPositions(t)
    PointInTimeData(t)
        -> UniverseResolver(t)
        -> RawFactorBatch(t, assets)
        -> HistoricalReferenceState(<t)
        -> FactorScorer(profile_version)
        -> AlphaAggregator(alpha_version)
        -> PortfolioConstructor(policy_version)
        -> RiskOverlay(risk_version)
        -> PendingOrders(execute >=t+1)
    RawFactorBatch(t) -> HistoricalReferenceState.update(t, after all scores)

职责边界如下：

1. 点时数据层只回答在 t 时点已知什么。
2. 股票池层决定 t 日哪些股票有资格被评分和交易，并保留被排除原因。
3. 原始因子层批量计算 raw_value，不知道 0–100、策略权重或仓位。
4. 历史状态层维护每个评分档案需要的全市场、行业和个股历史参考。
5. 因子评分层将一个原始值映射成统一方向的 0–100。
6. alpha 层只组合因子分数，输出策略评分，不决定持仓。
7. 组合层根据评分、容量和约束产生目标权重。
8. 风险层修改目标敞口，但不得修改原始策略评分。
9. 执行层模拟下一可交易时点成交。

由此得到一个关键不变量：同一 alpha 使用不同止盈、刹车或 rebalancer 时，同一股票同一天的策略评分必须完全相同。

## 5. 核心数据契约

### 5.1 RawFactorObservation

每个原始指标必须返回以下字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| asset_code | string | 证券代码 |
| as_of | date | 原始值所属交易日 |
| raw_metric_id | string | 稳定原始指标名 |
| raw_value | float/null | 未映射原始值 |
| raw_unit | string | percent、ratio、annualized_vol 等 |
| source_max_date | date | 参与计算的数据最大日期，必须小于等于 as_of |
| available_at | datetime/date | 该数据在现实中可获得的时间 |
| valid | bool | 原始值是否有效 |
| missing_reason | enum/null | 样本不足、字段缺失、非正分母、未披露等 |
| lookback_observations | int | 实际使用样本数 |
| raw_fingerprint | string | 算法、参数、价格口径和代码版本指纹 |
| diagnostics | object | 辅助量，例如 beta、bandwidth、负收益样本数 |

原始值缺失时 raw_value 必须为 null。禁止使用 0 代替缺失。

### 5.2 FactorScoreObservation

| 字段 | 类型 | 说明 |
|---|---|---|
| profile_id/profile_version | string/int | 不可变评分档案 |
| raw_observation_ref | key | 对应原始观测 |
| absolute_score | float/null | 固定锚点分量 |
| market_history_score | float/null | 全市场过去样本分量 |
| industry_history_score | float/null | 行业过去样本分量 |
| self_history_score | float/null | 本股票过去样本分量 |
| score | float | 最终 0–100 因子分 |
| evidence_coverage | float | 有效映射权重占比，0–1 |
| maturity | immature/partial/mature | 历史成熟状态 |
| reference_cutoff | date | 历史状态最大日期，正常应为上一交易日 |
| history_n | object | 各历史分量实际样本数 |
| state_hash | string | 所用历史状态摘要 |
| mapping_fingerprint | string | 完整评分配置指纹 |
| warnings | list | 截断、行业缺失、历史不足等 |

### 5.3 StrategyScoreObservation

必须至少包含 strategy_score、factor_scores、configured_weights、effective_coverage、score_status、alpha_fingerprint、mapping_fingerprints、universe_status 和 reference_cutoff。展示层直接使用 strategy_score，禁止再次用 score_full 映射。

所有数值保留内部双精度，持久化至少 6 位小数；UI 最后一步才四舍五入为整数或一位小数。

## 6. 因子评分算法

### 6.1 固定锚点等比例映射

一个固定映射由有序 knots 定义，每个点是 raw_value 到 score 的对应关系。相邻锚点之间使用线性插值；低于最小锚点或高于最大锚点时使用端点分数。映射必须单调，除非档案明确声明为峰形或谷形因子。

示例：Beta 因子“低 Beta 更好”的锚点为 beta 0 -> 100、beta 1 -> 50、beta 2 -> 0。beta 0.8 得到 60，beta 1.2 得到 40。这里 50 的含义来自经济中性 beta=1，不来自样本中位数。

每个档案必须显式声明：knots、方向、截断规则、单位、自然中点依据和版本。修改任何一个值都创建新版本，不得覆盖旧版本。

### 6.2 过去样本经验分位

对当前原始值 x，历史状态 H 只含 t-1 及以前的合格观测。定义 L 为 H 中小于 x 的数量，E 为等于 x 的数量，N 为总数，使用中秩：

    percentile = (L + 0.5 * E) / N

高值更好时 history_score = 100 * percentile；低值更好时 history_score = 100 * (1 - percentile)。最终截断到 0–100。

中秩保证离散值和大量相同值不会按股票顺序随机打散。禁止使用 t 日其他股票的当前值临时排名，也禁止把当前 x 先写入 H 再计算。

### 6.3 混合映射

档案配置理论权重 w_abs、w_market、w_industry、w_self，总和必须为 1。每个历史分量有成熟系数 m_j，成熟时为 1，样本不足时位于 0–1。最终分数为：

    score = 50 + sum(w_j * m_j * (component_score_j - 50))
    evidence_coverage = sum(w_j * m_j)

不可用分量等价于不提供证据，因此只让结果向 50 收缩，不把缺失声称为中性事实。raw_value 本身缺失时因子 score 记录为 50、evidence_coverage=0、maturity=immature，并带明确 missing_reason；关键因子缺失时组合层禁止开仓。

历史成熟系数默认采用 min(1, N/min_observations)，但正式评价期是否允许 partial 必须由档案的 formal_requires_mature 决定。默认关键因子要求 mature。

### 6.4 50 的来源优先级

1. 数学/经济自然中点：收益 0、Beta 1、布林位置 0.5、RSI 50、量比 1、趋势标准化值 0。
2. 有行业结构差异时，使用行业长期参考中位数。
3. 没有自然中点时，使用全市场过去样本中位数。
4. 个股自身中位数只能作为混合分量，不能单独决定跨股票评分。

### 6.5 绝对锚点不是历史最高和最低

动态 min/max 很容易被异常值和新极值拉动，并使过去同一原始值的语义漂移。因此：

- fixed knots 由经济含义、稳健历史分位和专题验证确定，随档案版本冻结。
- observed_min/max、P01/P05/P25/P50/P75/P95/P99 每期记录，但只用于诊断。
- 如果长期监控发现大量值越界，发布新档案版本并重新完整回测；不得静默移动旧锚点。

## 7. 因子评分档案

窗口必须属于因子评分档案，而不是散落在策略 YAML 中。策略只引用 profile_id。相同原始算法但不同窗口是不同 profile，例如 momentum_20d_v1 与 momentum_120d_v1。

建议配置结构：

    profile_id: dividend_yield_ttm_v1
    version: 1
    raw_metric:
      id: dividend_yield_ttm
      params:
        price_basis: raw
        trailing_days: 365
    direction: higher_is_better
    mapping:
      mode: hybrid
      components:
        absolute:
          weight: 0.50
          knots: [[0, 0], [1, 20], [2, 40], [3, 50], [5, 75], [8, 90], [12, 100]]
        market_history:
          weight: 0.25
          state: rolling
          years: 10
          sampling: month_end_cross_section
          min_observations: 3000
        industry_history:
          weight: 0.15
          state: rolling
          years: 10
          sampling: month_end_cross_section
          min_observations: 300
        self_history:
          weight: 0.10
          state: rolling
          years: 5
          sampling: month_end
          min_observations: 24
    formal_requires_mature: true
    missing_policy: shrink_to_50_and_block_if_critical
    valid_from: 2007-01-01

配置加载时必须校验权重和为 1、锚点单调、窗口为正、min_observations 可达、单位一致、profile_id 与 fingerprint 唯一。策略级 override 不得原地修改档案；有改动就生成新 profile_id。

## 8. 历史状态和窗口

### 8.1 默认窗口原则

- fixed 类型不需要历史窗口。
- 快速价格/技术因子：原始指标窗口由因子定义；历史参考默认 rolling 3 年，按日更新，自身状态保留精确日值，全市场/行业状态采用每周末完整横截面采样。
- 波动、偏度、Beta、流动性：历史参考默认 rolling 5 年，每周末完整横截面采样；自身状态按日。
- PE、PB、股息率、规模等慢变量：全市场和行业默认 rolling 10 年、月末横截面采样；自身默认 rolling 5 年、月末采样。
- 稀有事件计数：默认 rolling 5 年，避免制度或涨跌停规则变化被永久混入。

rolling 状态从已有样本开始扩张，达到配置窗口后再逐出最旧样本，即 expanding-until-full-then-rolling。若研究明确需要 expanding，档案可以设置 state=expanding；这仍然要求逐日追加，不允许用最终样本一次性拟合。

### 8.2 为什么不让所有因子都 expanding

2007 年的估值、换手和市场结构永久占据参考分布会使 2026 年的分数反应迟钝。rolling 更适合存在制度与结构漂移的因子；数学固定锚点负责跨时期稳定，rolling 历史分量负责适应当前制度。expanding 只用于分布相对稳定、样本稀少或研究明确要求全历史语义的档案。

### 8.3 采样与重复值

PE、PB 和股息率可能数月不变，若逐日把同一个披露值写入历史，会让“持续时间”替代“独立观察”主导分布。因此慢变量仅在月末或原始值发生变化时采样。快变量按日产生自有历史，全市场/行业池按周末完整截面采样，以控制状态规模且让每个截面权重一致。

采样规则必须确定性，只由日期、股票、档案决定。禁止随机 reservoir 造成重跑差异。

### 8.4 市场与行业历史池

历史池只能接收当时属于点时股票池、已上市、未退市且原始值有效的证券。不得用今天仍存续的股票回填过去。行业分类也必须使用 t 时点可见分类；若只有当前行业分类，industry_history_score 暂时禁用并把权重收缩到 50，不能伪装成点时正确。

ETF 与股票必须使用不同 market_scope；A 股主板、创业板、科创板可在同一市场池内，但行业与板块分量可以控制结构差异。指数本身不得混入个股历史池。

## 9. 回测时间协议

### 9.1 初始 1/5 观察期

给定已经按交易日排序并与策略有效数据范围相交的 dates，共 N 天：

    observation_count = ceil(N * 0.20)
    observation_dates = dates[0:observation_count]
    formal_scoring_dates = dates[observation_count:N]

观察期内完整执行股票池、原始值、评分和状态更新，保存诊断，但强制订单为空、持仓为空、现金不变。观察期最后一天仍不得生成订单。首个 formal_scoring_date 可以生成订单，最早在下一交易日执行，所以真实第一笔成交通常比 1/5 边界晚一个交易日。

如果关键档案在边界仍未达到 min_observations，继续保持 no-trade，直到成熟，并在结果中记录 maturity_delay_days。不得为了凑满后 4/5 偷降样本门槛。

### 9.2 指标预热与观察期不同

为了在 dates[0] 计算 252 日原始指标，可以读取 dates[0] 以前的价格作为 raw lookback；这些数据只用于生成当天 raw_value，默认不进入本次历史分位状态。若要把更早数据作为历史校准，必须把 history_origin 明确前移并写入 run manifest，不能隐式使用。

### 9.3 每日严格顺序

对每个交易日 t：

1. 在 t 日开盘或配置的成交时点，处理由 t-1 或更早信号产生的待执行订单；订单只能读取成交时点已经可见的数据。
2. 解析 t 日点时股票池；到收盘评分时，只加载 reference_cutoff < t 的历史状态。
3. 在 t 日评分数据全部可用后，批量计算全部股票 t 日 raw_value。
4. 使用步骤 2 的同一状态为全部股票评分；资产遍历顺序不能影响结果。
5. 聚合策略评分并保存审计记录。
6. 若处于观察期或成熟门禁未通过，跳过下单。
7. 否则组合和风险层产生目标，创建 execute_not_before 为下一交易日的待执行订单。
8. 所有 t 日评分完成后，将合格 t 日观测一次性追加到历史状态。
9. 写入状态 checkpoint，新的 reference_cutoff=t；进入下一交易日后再回到步骤 1 执行待处理订单。

任何在步骤 4 前更新 t 日状态的实现都是未来信息泄漏。任何使用 t 日收盘产生信号并以同一收盘无摩擦成交的实现都必须被测试拒绝。

### 9.4 运行起点与可比性

同一 history_origin、档案版本和输入数据下，延长回测结束日不得改变既有日期的任何分数。改变起始日会改变历史状态，必须视为不同校准域，不得直接比较同日分数。

正式研究应为每个策略族指定 canonical_history_origin 和 canonical_evaluation_start。临时短区间回测优先从同档案的标准 checkpoint 恢复，而不是重新用短区间前 1/5 校准。实时评分也必须延续 canonical 状态，才能与正式回测一致。

## 10. 策略聚合与缺失处理

### 10.1 聚合公式

每个 factor_score 已统一为越高越好。alpha 权重必须非负，除非明确实现一个相反方向的新因子档案。配置权重 a_i 后：

    strategy_score = sum(a_i * factor_score_i) / sum(a_i)
    coverage = sum(a_i * factor_evidence_coverage_i) / sum(a_i)

这样权重表示相对重要性，不再表示旧算子分数的放大倍数。FactorScore 已经按自身 evidence_coverage 向 50 收缩，策略层不得再乘一次 coverage，避免双重收缩。所有因子有效且成熟时 coverage=1。缺失因子的 score=50 会使最终分数自然向 50 收缩，而不是对剩余因子重归一化后制造极端高分；coverage 作为交易门禁和解释字段独立保留。

如果某因子 raw_value 缺失，聚合计算中使用 50，但 factor_evidence_coverage=0。策略还需声明 critical_factors、minimum_coverage 和 minimum_valid_factor_count；默认 minimum_coverage=0.70，关键因子缺失时 score_status=not_tradable，即使仍输出数值供展示也不得下单。

### 10.2 分数标签

标签只用于解释，不决定数学评分。V2 初始统一为：0–20 strongly_unfavorable、20–40 unfavorable、40–60 neutral、60–80 favorable、80–100 strongly_favorable。若策略要用不同买卖阈值，应属于组合政策，不能改变 strategy_score。

### 10.3 横截面和时序含义

- alpha score 负责表达适配度。
- 横截面组合可以选 score 最高的 N 只、超过阈值的股票或按 score-50 分配风险预算。
- 时序组合可以要求单股 score 超过绝对阈值才持有。
- 即使组合使用排名，排名也只用于资金竞争，不能反向覆盖 factor_score 或 strategy_score。

## 11. PE、PB 与股息率专题设计

### 11.1 不再直接用 PE 分位作为 value

V2 将 value 拆成两个独立原始指标和评分档案：

1. earnings_yield_ttm = 1 / PE_TTM，优先直接从点时利润和市值计算。盈利为负时保留负的盈利收益率，代表差的盈利支撑，不再当缺失。PE 为 0、异常或数据不可解释时才缺失。
2. book_to_price = 1 / PB，优先直接从点时归母净资产和市值计算。负净资产产生负 B/P，作为风险/低质量证据；PB=0 或口径错误才缺失。

候选绝对锚点：

- earnings_yield：-10% -> 0，0% -> 20，5% -> 50，10% -> 80，20% -> 100。
- book_to_price：-0.5 -> 0，0 -> 20，0.5 -> 50，1.0 -> 80，2.0 -> 100。

两者默认采用 absolute 0.40、market history 0.20、industry history 0.30、self history 0.10；市场/行业 rolling 10 年月末采样，自身 rolling 5 年月末采样。行业占比较高是因为银行、周期、科技的合理 PE/PB 结构不同。

最终“价值策略”在 alpha 层分别引用两个档案，例如盈利收益率 0.6、B/P 0.4。不得再创建一个内部隐藏 PE、PB 和股息的 value 算子。

### 11.2 股息率

股息率继续使用过去 365 天点时已实施或已满足可用性规则的每股现金分红除以不复权价格。除息、公告和实施日期必须明确，禁止用后来知道的全年分红回填公告前日期。

股息率采用本文件第 7 节示例的混合档案。绝对分量避免一个 0.2% 的股息率仅因市场普遍不分红而变成极高分；历史分量又避免机械规定 5% 必然 100。行业、自身历史分量让公用事业与成长股、同一公司高低股价时期都能被解释。

高股息陷阱不得通过扭曲股息率分数处理。分红可持续性、盈利质量、负债、现金流和一次性特别分红应是独立质量/风险因子，策略自行组合。

### 11.3 删除 graham_value

graham_value 不可复用为原子因子，V2 删除。若以后需要复现格雷厄姆防御策略，直接在 alpha 层组合 earnings_yield、book_to_price、dividend_yield 和质量约束；权重、阈值全部可见，避免股息重复计算。

## 12. 33 个现有因子的迁移设计

下表给出 V2 首版候选默认值。它们是实现起点，不代表已经通过收益研究；每个档案完成无未来数据的单因子验证后才可标记 active。

| V1 因子 | V2 原始值与方向 | 首版映射和历史窗口 | 迁移动作 |
|---|---|---|---|
| amplitude | 20 日 mean((high-low)/close)，低更好 | 混合：绝对 0.4、市场 0.3、行业 0.1、自身 0.2；历史 3 年，市场周末截面 | 保留 raw 算法，删除旧 30/70 分位死区 |
| bias_reversal | close/MA20-1，低更符合反转 | -15%->100、0->50、+15%->0 占 0.6，市场/自身各 0.2，3 年 | 保留 raw，重写映射 |
| daily_bollinger | 日线带宽位置，低更符合均值回归 | fixed：position 0->100、0.5->50、1->0，越界截断 | 保留布林 raw，删除旧非对称 score |
| distance_from_low | close/rolling_low252-1，低更符合低点反转 | 0->100、25%->50、100%->0 占 0.6，市场/自身各 0.2，5/3 年 | 保留 raw，重写映射 |
| dividend_yield | TTM 现金股息率，高更好 | 绝对 0.5、市场 0.25、行业 0.15、自身 0.1；10/10/5 年月末 | 保留正确 raw 口径，完全替换旧 1%–5% 机械分 |
| donchian_breakout | 不含当日的 N 日通道位置，高更符合趋势突破 | fixed：0->0、0.5->50、1->100；越界截断 | 保留 raw，统一方向和量纲 |
| downside_skewness | 60 日收益偏度，低更符合反彩票假设 | fixed -2->100、0->50、+2->0 占 0.5；市场/自身 0.3/0.2，5 年 | 保留 raw，重写映射并复核因子命名 |
| downside_volatility | 年化下行偏差，低更好 | 5%->100、20%->50、60%->0 占 0.4；市场/行业/自身 0.3/0.1/0.2，5 年 | 统一年化单位，替换自身分位死区 |
| fifty_two_week_high | close/rolling_high250，高更符合趋势 | fixed：0.5->0、0.75->40、0.9->70、1->100 | 保留 raw；旧策略结论归档后重新验证 |
| graham_value | 无单一 raw，混合了 PE/PB/股息 | 不再提供 | 删除；由三个透明因子在 alpha 层复现 |
| illiquidity | log(mean(abs(return)/amount))，高代表流动性溢价 | 市场 0.5、行业 0.3、自身 0.2，rolling 5 年周末截面；极端不可交易另行过滤 | 保留 Amihud raw，删除任意 log 常数打分 |
| intraday_return | 20 日平均 close/open-1，高更好 | 0 为 50 的 fixed 占 0.5，市场/自身 0.3/0.2，3 年 | 保留 raw，替换 30/70 死区 |
| limit_up_count | 20 日按板块/ST 规则校正的涨停次数，低更好 | 单边惩罚：0->55、1->45、2->25、3+->0；制度版本入指纹，5 年诊断 | 保留事件 raw；不把“没涨停”夸成强 alpha |
| lottery_max | 20 日最大单日收益，低更好 | 单边惩罚：3%及以下->60、5%->50、8%->20、12%->0；市场历史只作诊断 | 保留 raw，删除旧 -20..0 量纲 |
| low_beta | 相对指定基准的 120 日 beta，低更好 | fixed：0->100、1->50、2->0 占 0.7，市场历史 0.3，5 年 | 保留点时对齐回归，统一 0–100 |
| low_turnover | 20 日平均换手率，低更好 | 0.2%->90、0.5%->75、2%->50、5%->20、10%->0 占 0.4；市场/行业/自身 0.3/0.2/0.1，5 年 | 保留 raw；交易容量另设硬门禁 |
| low_volatility | 20 日年化收益波动率，低更好 | 10%->90、25%->50、60%->0 占 0.4；市场/行业/自身 0.3/0.1/0.2，5 年 | 保留 raw，纠正“只比自己历史就叫低波” |
| macd_cross | 日/周 MACD histogram 经 ATR 或价格波动归一的连续 spread，高更好 | 各周期独立档案，z -2->0、0->50、+2->100；cross_age 仅作诊断 | 拆成 macd_daily_strength 与 macd_weekly_strength；删除离散旧算子 |
| mean_reversion | RSI14，低更符合反转 | 与 rsi_reversal 合并；RSI 0->100、50->50、100->0，占 0.7，市场/自身 0.15/0.15 | 删除重复实现，迁移到 rsi_level_reversal |
| momentum | N 日对数收益，高更符合动量 | 每个 N 独立档案；0 为 50，20 日默认 -20%->0、+20%->100 占 0.5，市场/行业/自身 0.25/0.1/0.15，3 年 | 保留 return raw，删除无界 ret*2 score |
| momentum_acceleration | 后半窗收益减前半窗收益，高更好 | 0 为 50；-15%->0、+15%->100 占 0.6，市场/自身各 0.2，3 年 | 保留 raw，重写映射 |
| monthly_bollinger | 月线布林位置，低更符合均值回归 | fixed 0->100、0.5->50、1->0；档案明确月末聚合和未完月规则 | 保留 raw，删除旧 score |
| overnight_return | 20 日平均 open/previous_close-1，低更符合反转 | 0 为 50；-1%->100、+1%->0 占 0.5，市场/自身 0.3/0.2，3 年 | 保留 qfq raw，替换自身分位死区 |
| residual_reversal | 每日在当时 60 日 beta 下得到的 20 日残差累计/均值，低更好 | 0 为 50 的 fixed 0.5，市场/自身 0.3/0.2，3 年 | 重写历史 raw，禁止用当前 beta 重算整段历史 |
| reversal | 与 N 日收益相反方向 | 复用 momentum 的 raw_metric，只引用 direction=lower_is_better 的独立 profile | 删除重复 raw 文件，保留可解释的反转档案名 |
| rsi_reversal | RSI14，低更符合反转 | 见 rsi_level_reversal | 与 mean_reversion 合并，旧两套评分均删除 |
| size | 点时自由流通/总市值的 log 值，低更符合小盘因子 | 绝对 0.3、市场 0.35、行业 0.25、自身 0.1；10/10/5 年月末 | 只保留可靠市值 raw；amount/turnover 代理必须单列低质量源或停用 |
| trend_linearity | signed_r2 = sign(log-price slope)*R2，高更好 | fixed：-1->0、0->50、+1->100 | 保留回归 raw，值中必须含方向而非只存 R2 |
| trend_strength | 波动归一的 MA5/10/20 间距连续值，高更好 | z -2->0、0->50、+2->100；3 年历史只作稳健监控 | 重写 raw；删除 {-20,0,20} 三档算子 |
| ts_momentum | N 日收益/(日波动*sqrt(N))，高更好 | fixed：z -2->0、0->50、+2->100 | 保留 raw，替换旧正负 20 score |
| value | 当前实际为 PE 自身历史分位 | 拆为 earnings_yield 与 book_to_price，见第 11 节 | 删除旧 value 算子和缓存，不设兼容别名 |
| volume_drought | MA(amount,5)/MA(amount,120)，低更符合缩量反转 | fixed：0.3->100、1->50、2->0 占 0.6，市场/自身各 0.2，3 年 | 保留 raw，重写映射 |
| weekly_bollinger | 周线布林位置，低更符合均值回归 | fixed 0->100、0.5->50、1->0；明确周末聚合 | 保留 raw，删除旧非对称 score |

上述锚点在实现前必须用观察期 raw 分布报告检查 P01/P05/P50/P95/P99 和截断率。调整锚点会创建 v2、v3 档案，不能偷改 v1。

## 13. 新策略模型

### 13.1 三种独立身份

- alpha_id：因子档案、因子权重、关键因子和覆盖门槛。决定 strategy_score。
- portfolio_policy_id：top N、阈值、最大单股、行业上限、换仓频率、锁仓等。决定目标仓位。
- risk_policy_id：止损、止盈、回撤刹车、regime、总敞口和波动率目标。只修改敞口。

一个可执行策略实例由三者组合：deployment_id = alpha_id + portfolio_policy_id + risk_policy_id。相同 alpha 的多个 deployment 不再复制因子列表。

### 13.2 候选 alpha 家族

V2 不自动重建 52 个旧名字。先保留以下研究家族，每个都必须重新全周期回测：

1. dividend_quality_value：dividend_yield、earnings_yield、book_to_price、low_volatility，可加入独立质量因子。
2. reversal_value：reversal_20d、rsi_level_reversal、earnings_yield、book_to_price。
3. momentum_trend：momentum、trend_linearity、trend_strength，可按股票与 ETF 分开档案。
4. bollinger_reversion：daily/weekly/monthly bollinger 与 RSI，不把趋势突破和均值回归混在同一方向。
5. defensive：low_volatility、downside_volatility、low_beta、anti_lottery。
6. liquidity_size：size、low_turnover、illiquidity、reversal，并用交易容量硬门禁。
7. 单因子研究 alpha：每个新因子单独验证，不默认进入生产策略。

旧 graham、pure_factor、smart_beta 等复合名只作为历史记录；如果其思想仍有价值，用透明的新因子重新创建新 alpha_id。

## 14. 状态、缓存和持久化

### 14.1 分离纯原始缓存和有路径依赖的评分

V2 至少需要以下逻辑存储：

1. factor_raw_cache：自然键 asset_code、as_of、raw_metric_id、raw_fingerprint；只存纯 raw 结果和数据截止日，可跨策略复用。
2. factor_profile_registry：不可变配置、版本、fingerprint、状态和创建原因。
3. factor_score_audit：run_id、日期、股票、profile、各组件分、最终分、成熟度、state_hash。
4. factor_state_checkpoint：run_id、profile、reference_cutoff、窗口内容或可恢复摘要、校验和。
5. strategy_score_audit：alpha 分、因子分、覆盖率、可交易状态。
6. backtest_run_manifest：所有重现运行所需元数据。

旧 operator_result 中带旧 score 的记录不迁移。若某旧计算函数被提取为 V2 raw，也必须用新的 raw_metric_id/fingerprint 重新生成，避免把旧 value/score 语义误当原始值。

### 14.2 Checkpoint 要求

checkpoint 必须包含：profile fingerprint、history_origin、reference_cutoff、市场池版本、行业映射版本、每个历史池样本数、rolling 队列必要内容、分位摘要、observed min/max、随机性声明和 checksum。恢复后下一日输出必须与不间断运行逐位一致。

全市场/行业状态可以使用确定性的排序数组、分桶或可删除的 quantile 结构，但必须满足：插入顺序不改变结果、rolling 能精确移除过期采样日、同值采用中秩、checkpoint 可无损恢复。首版优先正确性，不要先引入不可验证的近似 t-digest。

### 14.3 Run manifest

每次运行必须保存：run_id、git commit、数据快照/最大日期、请求起止日、history_origin、observation_count、formal_start、真实首单日、股票池定义与版本、退市股处理、所有 profile/alpha/policy/risk fingerprint、费用滑点、成交价规则、再平衡日历、初始资金、并发参数、状态 checkpoint 来源和输出校验和。

## 15. 回测结果协议

同一运行输出分成两个互不混淆的区间：

- observation：只报告原始值覆盖、历史成熟曲线、分位锚点、截断率、缺失率和分数稳定性；收益指标为空。
- formal：报告收益、风险、交易、暴露和因子诊断。净值基准从正式期开始统一设为 1，观察期不计作零收益投资期。

结果摘要必须额外包含 maturity_delay_days、score_coverage、factor_missing_rate、factor_clamp_rate、score P01/P05/P50/P95/P99、横截面唯一值比例、0/100 饱和比例、行业暴露、股票池存活偏差检查、实际首单和末单日期。

全周期结果之外，至少按牛/熊/震荡、年份、行业、上市年龄和市值组分段；但所有分段都来自同一次连续状态运行，禁止每段重新校准后拼接收益。

## 16. 防未来函数规则

以下规则都是硬失败，不是警告：

1. 原始行情 source_max_date > as_of。
2. 基本面、分红使用公告/实施之前不可获得的值。
3. t 日历史分位状态含 t 日任意股票观测。
4. 用今天的行业、成分股或存续股票列表回填过去。
5. 用回测结束后的全样本 min/max/quantile 评分过去。
6. t 日收盘信号以同一收盘成交，除非数据和撮合模型明确证明信号在成交前可得。
7. 恢复 checkpoint 后加载了 checkpoint cutoff 之后的数据。
8. 因延长 end date 而改变已有日期分数。

最强回归测试是 prefix invariance：相同起点分别运行到 T1 和 T2，T2>T1，比较截至 T1 的 raw、factor score、strategy score、订单、成交和状态 hash，必须完全相同。

## 17. V1 策略归档、清除与复现门禁

### 17.1 最终处置决定

52 份 stockfu/ai/strategies/*.yaml 不作为 V2 活跃配置复用。原因是它们把旧因子量纲、alpha、仓位、debounce 和风险政策耦合在一起，即使文件名或部分参数看似可用，也会把 score_full 等旧假设带回新系统。归档必须同时区分“52 个源文件”“seed 选择的 29 个基础文件”和“当前保留的 31 个展开运行 id”。

迁移顺序必须是：完整归档 -> 校验可复现 -> 建立 V2 最小策略集 -> 对照验证 -> 删除 V1 源配置和不可复用代码。不得先删后补文档，也不得把整个旧目录改名为 legacy 后继续被运行时扫描。

### 17.2 必须生成的归档资料

实施阶段先创建 docs/legacy/strategy-v1/，其中至少包含：

| 文件 | 必须内容 |
|---|---|
| catalog.yaml | 52 个基础 YAML 和全部 variants 展开后的有效配置；所有隐式默认值都已展开 |
| catalog.md | 中文目的、因子假设、已知问题、历史结论、替代 V2 家族和复现命令说明 |
| strategy-source/ | 原 52 个 YAML 的只读文本副本，保留注释；不在运行时路径 |
| runtime-bindings.yaml | 每个策略曾使用的 rebalancer、参数、股票池、调度入口和默认 app_config |
| result-index.csv | 已知回测 artifact 路径、起止日、数据截止、主要指标、是否证伪 |
| checksums.sha256 | 上述文件和原 YAML 的 SHA-256 |
| migration-map.yaml | old_strategy_id -> archive_only 或 new alpha/policy/risk 组合；禁止模糊自动别名 |

catalog.yaml 中每个展开策略必须保存：

- 原 strategy_id、base_id、variant_key、中文名、source_path 和 source_sha256。
- operators 的原顺序、id、type、weight、params。
- aggregate method、thresholds、sell_weights。
- position、debounce、risk 的每个最终有效字段。
- YAML 未写但运行时会采用的默认值及其来源文件、来源键。
- active_rebalancer_id、完整 rebalancer_params、动态股票池、基准、初始资金、费用、滑点、成交价、调仓频率。
- 依赖的数据表和价格口径。
- 相关历史结果、注释里的已知结论和 docs/BACKTEST.md 章节。
- 最后能运行该配置的 git commit 和 Python/依赖版本。

仅保存 YAML 不足以复现，因为当前 rebalancer 和部分默认风险参数位于 YAML 外部。仅依赖 Git 历史也不足以作为业务归档，因为未来模型无法知道哪个提交、外部参数和数据快照共同构成一次结果。

### 17.3 删除门禁

只有同时满足以下条件才能删除 V1 活跃文件：

1. 归档脚本确认 52/52 基础文件已收录；另断言当前 seed 基础选择为 29、_RETAINED_STRATEGY_IDS 为 31，展开 variant 与筛选结果和 seed._expand_variants 一致。
2. 每个源文件 checksum 与归档副本一致。
3. 每个源策略都能由 catalog.yaml 重新渲染；31 个 retained 运行 id 还能渲染成与当前 seed 等价的有效配置对象，其他源策略明确标为 inactive/pruned。
4. 渲染结果逐字段等于当前 compile_strategy 加载后的配置，包括隐式默认值。
5. runtime-bindings 不再有 unknown rebalancer、unknown universe 或 unknown cost model；若历史信息确实不可找回，必须明确标成 unrecoverable，不得猜测。
6. result-index 已保存现有 artifact 路径和 checksum；缺失结果明确标记 not_available。
7. migration-map 覆盖所有 old id 和 variant id。
8. V2 CLI 遇到旧 id 时返回“已归档、请查 migration-map”的明确错误，不静默运行相似新策略。

### 17.4 删除对象

归档通过后：

- 删除 stockfu/ai/strategies 下 52 份 V1 YAML，目录只允许放 V2 schema 配置或改用新的 configs 路径。
- 删除 weighted_sum 的旧 score 求和契约；如文件继续存在，其实现必须是 0–100 加权平均且使用新类型，不能兼容 OpResult.score。
- 删除 score_full 字段、仓位换算和 signal_scan 二次映射。
- 删除 V1 value、graham_value、离散 macd_cross、离散 trend_strength、重复 mean_reversion/reversal raw 等不可复用实现。
- 清空或删除旧 operator_result score 缓存；不迁移旧 score。
- 删除只服务于旧 YAML seed/variant 展开的路径，V2 用明确的 alpha/policy/deployment registry。
- 删除过期的旧策略调度 catalog，保留的历史结果索引改读归档。

删除代码前先用 rg 搜索所有引用，逐一迁移 API、邮件、荐股、诊断和 CLI。禁止留下永远不执行但继续误导后续模型的兼容死代码。

## 18. 52 个 V1 策略的现状复现目录

本节记录当前显式配置，实施归档时还要按第 17 节展开运行时默认值。为避免重复并保持精确，先定义缩写。

### 18.1 公共配置缩写

- W12：weighted_sum；thresholds strong_buy=12、buy=4、hold=-4、sell=-12。
- W8：weighted_sum；thresholds 8、3、-3、-8。
- W10：weighted_sum；thresholds 10、4、-4、-10。
- D1：buy_cool_down_days=1、sell_cooldown_days=1、max_target_step=1.0、risk_confirm_days=1、min_trade_weight=0.01、conf_gate=0.0。
- Drot：buy=5 天、sell=3 天、max_target_step=0.3、risk_confirm_days=1、min_trade_weight=0.01、conf_gate=0.3。
- Dmacd：buy=3 天、sell=0 天、max_target_step=1.0、risk_confirm_days=1、min_trade_weight=0.01、conf_gate=0。
- Dhold：buy=30 天、sell=30 天、max_target_step=1.0、risk_confirm_days=1、min_trade_weight=0.01、conf_gate=0。
- P20：position continuous、max_w=0.20、dead=3、score_full=8。
- P05：position continuous、max_w=0.05、dead=3、score_full=8。
- Prot12：position continuous、max_w=0.12、dead=3、score_full=8。
- Prot10：position continuous、max_w=0.10、dead=3；YAML 未写 score_full，需展开运行时默认。
- R0：stop_loss=0、portfolio_brake=0、max_gross=1.0。
- Rimplicit：YAML 没有 risk；复现时必须从当时运行配置展开，不能把“未写”误认为关闭。

参数表达式 factor[weight; params] 完整保留原权重和显式参数。表中“意图”来自文件注释；真正 rebalancer 仍以 runtime-bindings 归档为准。

### 18.2 红利公共 alpha 与风险政策

红利公共 alpha H1 为：dividend_yield[1.0; high_yield=5.0, price_basis=raw, yield_cap=20.0] + low_volatility[0.8; window=20, hist_years=3] + value[0.6; years=5]，聚合 W12。

H2 为买入等权版本：三个因子权重均 1.0；aggregate 另有 sell_weights dividend_yield=2、low_volatility=1、value=2。

红利风险缩写：

| 编号 | 精确显式配置 |
|---|---|
| RH0 | risk 未写，需展开默认 |
| RHsl30 | stop_loss=0.30 |
| RHfull | stop_loss=0.30；trailing profit 0.20/drawdown 0.05、profit 0.30/drawdown 0.03；hard_profit=0.50 |
| RHpartial | stop_loss=0.30；上述两档 trailing 各 sell_fraction=1/3；hard_profit=null |
| RHatr | stop_loss=0.30；ATR20 两档：profit 0.20/multiple 2.0/sell_fraction 1/3，profit 0.30/multiple 1.25/sell_fraction 1/3；hard_profit=null |
| RHatrLag | 与 RHatr 相同，另 lagged=true |
| RHbrake | RHpartial + portfolio_brake=0.08 |
| RHgentle | RHpartial + portfolio_brake=0.08、portfolio_brake_scale=0.75 |
| RHselect | RHpartial + portfolio_brake=0.08、mode=block_new_buys、scale=1.0 |
| RHdeep | RHpartial + brake=0.08、scale=1.0、recover_high_days=63、tiers 8%/0.85、12%/0.75、20%/0.60、30%/0.45 |
| RHadd | RHpartial + brake=0.08、scale=1.20、add_min_score=12；简单版另 max_gross=1.00，分级版 tiers 8%/0.95、12%/0.80、20%/0.65、30%/0.50、recover=63 |
| RHtrend | RHdeep + sh000300、MA200、enter_band=0、exit_band=0.03、regime max_gross=0.50 |
| RHvol | RHdeep + sh000300、target_vol=0.15、vol_window=63、vol_floor=0.30 |
| RHtrendvol | RHtrend + target_vol=0.15、vol_window=63、vol_floor=0.30 |
| RHhold | max_gross=0.80、stop_loss=0.30、brake=0.08、scale=1.0、recover=63、tiers 8%/0.85、12%/0.75、20%/0.60、30%/0.45，加 RHpartial |
| RHaddHold | max_gross=0.80、stop_loss=0.30、brake=0.08、scale=1.20、add_min_score=12、recover=63、tiers 8%/0.95、12%/0.80、20%/0.65、30%/0.50，加 RHpartial |

### 18.3 全部基础策略

| # | V1 id / 中文名 | 因子与参数 | 显式组合、风险及迁移备注 |
|---:|---|---|---|
| 1 | amplitude / 低振幅横截面 | amplitude[1; defaults window20/hist3y] | W12、P20、D1、R0；单因子研究，归档后用 V2 amplitude 重建 |
| 2 | anti_lottery_defensive / 反彩票防御 | lottery_max[1; window20,warn5,flag8] + low_volatility[0.8;20,3y] + value[0.6;5y] | W12、P20、D1、R0；value 拆分后重新研究 |
| 3 | bias_reversal / 乖离率反转 | bias_reversal[1; window20,hist3y] | W12、P20、D1、R0；单因子研究 |
| 4 | bollinger_reversion / 日周布林均值回归 | daily_bollinger[1;20,2,0.45,0.55] + weekly_bollinger[0.6;20,2,0.45,0.55] + mean_reversion[0.4;RSI14,30,70] + trend_strength[0.2] | W8、Prot10、Drot、Rimplicit；意图 top_n，V2 重组 |
| 5 | bollinger_reversion_cross_section / 布林回归横截面 | 与 #4 相同 | W8、P05、D1、Rimplicit；意图 cap_and_rank |
| 6 | cn_momentum_cross_section / 个股动量横截面 | momentum[1;20] + trend_linearity[0.6;20] + trend_strength[0.4] | W12、P05、D1、Rimplicit |
| 7 | cn_momentum_rotation / 降换手动量轮动 | 与 #6 相同 | W12、Prot12、Drot；risk max_gross=.90, stop_loss=.08, brake=.10；注释记录 2021-01-04 至 2026-07-15 年化 4.34%、Sharpe .30、回撤 31.62%，通用策略已证伪，archive_only |
| 8 | cross_section_factor / 反转低波价值 | reversal[1;20] + low_volatility[.8;20,3y] + value[.6;5y] | W12、P05、D1、Rimplicit；V2 reversal_value 候选 |
| 9 | dividend_cross_section / 红利横截面 | H1 | P05、D1、RH0；variants：sl30 只设 stop_loss=.30；sl30w10 另 max_w=.10；sl30w20 另 max_w=.20 |
| 10 | dividend_cross_section_atr_lagged_take_profit / 滞后 ATR 止盈 | H1 | P05、D1、RHatrLag |
| 11 | dividend_cross_section_atr_take_profit / ATR 止盈 | H1 | P05、D1、RHatr |
| 12 | dividend_cross_section_partial_brake_take_profit / 分段止盈加组合刹车 | H1 | P05、D1、RHbrake |
| 13 | dividend_cross_section_partial_drawdown_add_gated_take_profit / 回撤加仓质量门控 | H1 | P05、D1；RHadd 简单版，含 max_gross=1.00 |
| 14 | dividend_cross_section_partial_exposure_add_gated_hold_take_profit / 分级敞口、加仓门控及不对称滞回 | H2 | position max_w=.05,dead=5,score_full=8；Dhold；RHaddHold |
| 15 | dividend_cross_section_partial_exposure_add_gated_take_profit / 分级敞口与加仓门控 | H1 | P05、D1；RHadd 分级版 |
| 16 | dividend_cross_section_partial_exposure_brake_hold_take_profit / 分级敞口与不对称滞回 | H2 | position max_w=.05,dead=5,score_full=8；Dhold；RHhold |
| 17 | dividend_cross_section_partial_exposure_brake_regime_trend_take_profit / 深度刹车加趋势 regime | H1 | P05、D1、RHtrend |
| 18 | dividend_cross_section_partial_exposure_brake_regime_trendvol_take_profit / 趋势波动双 regime | H1 | P05、D1、RHtrendvol |
| 19 | dividend_cross_section_partial_exposure_brake_regime_vol_take_profit / 波动率定权 | H1 | P05、D1、RHvol |
| 20 | dividend_cross_section_partial_exposure_brake_take_profit / 深度分级敞口刹车 | H1 | P05、D1、RHdeep；variant deep 改 tiers 为 15%/.75、25%/.55、35%/.40；variant rec125 改 recover_high_days=125 |
| 21 | dividend_cross_section_partial_gentle_brake_take_profit / 平滑组合刹车 | H1 | P05、D1、RHgentle |
| 22 | dividend_cross_section_partial_selective_brake_take_profit / 选择性组合刹车 | H1 | P05、D1、RHselect |
| 23 | dividend_cross_section_partial_take_profit / 分段减仓止盈 | H1 | P05、D1、RHpartial |
| 24 | dividend_cross_section_take_profit / 分级追踪止盈 | H1 | P05、D1、RHfull |
| 25 | dividend_low_vol / 红利低波轮动 | H1 | W12、Prot12、Drot、Rimplicit；意图 top_n lock20 |
| 26 | donchian_breakout_cross_section / 唐奇安突破 | donchian[1;20,.8,.2] + ts_momentum[.6;120] + trend_strength[.4] | W12、P20、D1；stop=.18,gross=1,brake=0；ATR20 止盈 10%/2.5x/卖1/2、20%/2x/卖1/2 |
| 27 | dual_bollinger / 双布林 | weekly_bollinger[1;20,2] + monthly_bollinger[.8;20,2] + momentum[.4;20] | W10、Prot10、Drot、Rimplicit；均值回归与动量方向需重新审视 |
| 28 | etf_momentum_cross_section / ETF 动量横截面 | momentum[1;20] + trend_linearity[.6;20] + trend_strength[.4] | W12、P05、D1、Rimplicit；ETF market_scope 单列 |
| 29 | etf_momentum_rotation / ETF 动量轮动 | 与 #28 相同 | W12；position max_w=.18,dead=3,score_full=8；Drot、Rimplicit；意图 top5 lock20 |
| 30 | fifty_two_week_high_cross_section / 52 周新高 | fifty_two_week_high[1;250,.70] + momentum[.6;120] + trend_strength[.4] | W12、P20、D1、R0；旧注释称满仓配置已证伪，先 archive_only |
| 31 | graham_defensive_value / 格雷厄姆防御价值 | graham_value[1;5y] + dividend_yield[.6;5%,raw,cap20] + low_volatility[.5;20,3y] | W12、P20、D1、R0，另 hard_profit=.50；存在股息重复，旧 alpha 不复用 |
| 32 | illiquidity_value / Amihud 流动性价值 | illiquidity[1;20] + value[.8;5y] + reversal[.5;20] | W12、P20、D1、R0；需加容量硬门禁后重建 |
| 33 | intraday_return / 日内动量 | intraday_return[1; defaults 20,3y] | W12、P20、D1、R0；单因子研究 |
| 34 | limit_up_count / 涨停计数反转 | limit_up_count[1; defaults] | W12、P20、D1、R0；V2 作为单边惩罚研究 |
| 35 | low_beta_dividend / 低 Beta 红利 | low_beta[1;120,sh000300] + dividend_yield[.8;5%,raw,cap20] + value[.6;5y] | W12、P20、D1、R0；value 拆分后重建 defensive 家族 |
| 36 | low_downside_vol / 低下行波动 | downside_volatility[1;60,3y] | W12、P20、D1、R0；单因子研究 |
| 37 | low_skewness / 负偏度防御 | downside_skewness[1;60,3y] | W12、P20、D1、R0；单因子研究，因子方向需实证 |
| 38 | low_turnover_reversal / 低换手反转价值 | low_turnover[1;20] + reversal[.8;20] + value[.6;5y] | W12、P20、D1、R0；V2 liquidity_size 候选 |
| 39 | macd_cross / MACD 金叉死叉 | macd_cross[1;12,26,9] | thresholds 8,4,-4,-8；position max_w=.10,dead=3,score_full=10；Dmacd、Rimplicit；离散因子删除后按日/周连续强度重建 |
| 40 | momentum_breakout / 月线动量突破 | monthly_bollinger[1.2;20,2] + momentum[.6;20] + trend_strength[.4] | W10、Prot10、Drot、Rimplicit；旧注释确认实质为动量追涨，不是布林回归 |
| 41 | momentum_breakout_cross_section / 动量突破横截面 | 与 #40 相同 | W10、P05、D1、Rimplicit |
| 42 | near_52w_low / 52 周低点反转 | distance_from_low[1;252,3y] | W12、P20、D1、R0；单因子研究 |
| 43 | overnight_reversal / 隔夜反转 | overnight_return[1;20,3y] | W12、P20、D1、R0；单因子研究 |
| 44 | pure_factor / 纯因子动量反转 | momentum[1.5;20] + mean_reversion[1;RSI14,30,70] + trend_strength[1] + value[.8;5y] | W12；position max_w=.10,dead=2.5,score_full 隐式；Dmacd、Rimplicit；因子方向互相冲突，archive_only |
| 45 | residual_reversal / 残差反转 | residual_reversal[1; defaults 20,beta60,hist3y] | W12、P20、D1、R0；修复历史 beta 口径后重建 |
| 46 | reversal_cross_section / 反转横截面 | reversal[1;20] + mean_reversion[.6;RSI14,30,70] + value[.4;5y] | W12、P05、D1、Rimplicit |
| 47 | reversal_strategy / 反转轮动 | 与 #46 相同 | W12、Prot12、Drot、Rimplicit；意图 top8 lock20 |
| 48 | rsi_reversal / RSI 超卖反转 | rsi_reversal[1;14,3y] | W12、P20、D1、R0；与 mean_reversion 合并后研究 |
| 49 | small_cap_low_turnover / 小盘低换手 | size[1; default20] + low_turnover[.8;20] + reversal[.5;20] | W12、P20、D1、R0；可靠市值与容量门禁就绪后重建 |
| 50 | smart_beta_multi_factor / 智能贝塔多因子 | 52w_high[.6;250,.7] + low_volatility[.8;20,3y] + value[.6;5y] + low_turnover[.6;20] + graham_value[.4;5y] | W12、position max_w=.20,dead=3,score_full=10、D1、R0；value/graham 重复，archive_only |
| 51 | ts_momentum_trend / 时序动量趋势 | ts_momentum[1;120] + momentum_acceleration[.6;120] + trend_strength[.4] | W12、P20、D1、R0；另 target_vol=.15,vol_window=63,vol_floor=.30；V2 momentum_trend 候选 |
| 52 | volume_drought / 量能枯竭反转 | volume_drought[1;short5,long120,hist3y] | W12、P20、D1、R0；单因子研究 |

### 18.4 重复关系必须在迁移后消失

- #9–#25 的绝大多数共享 H1，V2 只允许一个 dividend_quality_value alpha；止盈/刹车是 policy/risk 组合。
- #6、#7、#28、#29 共享同一动量三因子，股票与 ETF 因 market_scope 分两个 alpha profile，但轮动和横截面不复制因子。
- #4/#5、#40/#41、#46/#47 分别是相同 alpha 的组合政策对照。
- 单因子文件不需要各自成为“生产策略”；V2 factor diagnostic 可直接运行单因子 alpha 模板。

## 19. V2 配置示例

alpha 配置只表达评分：

    alpha_id: dividend_quality_value_v1
    version: 1
    market_scope: cn_equity
    factors:
      - {profile_id: dividend_yield_ttm_v1, weight: 1.0, critical: true}
      - {profile_id: low_volatility_20d_v1, weight: 0.8, critical: false}
      - {profile_id: earnings_yield_ttm_v1, weight: 0.35, critical: true}
      - {profile_id: book_to_price_v1, weight: 0.25, critical: false}
    minimum_coverage: 0.70
    minimum_valid_factor_count: 2

组合配置只表达如何持有：

    portfolio_policy_id: cn_equity_top20_v1
    rebalance: weekly
    selection: {method: top_n_above_score, n: 20, minimum_score: 60}
    weighting: equal
    max_single_weight: 0.05
    max_industry_weight: 0.25
    max_gross: 0.95
    min_amount_20d: 50000000
    minimum_listing_days: 252

风险配置只表达风险覆盖：

    risk_policy_id: no_overlay_v1
    stop_loss: null
    take_profit: null
    drawdown_brake: null
    volatility_target: null

上述三份配置分别 fingerprint。任何改变都创建新版本；回测结果以 deployment fingerprint 为主键，不能只存一个易重名的 strategy_id。

## 20. 实现模块与文件处置图

建议创建以下职责明确的模块；实际目录名可以小幅调整，但边界不得合并回单体 runner：

    stockfu/scoring/contracts.py          数据类、枚举、校验
    stockfu/scoring/profiles.py           因子档案加载、版本和 fingerprint
    stockfu/scoring/mappings.py           fixed、ECDF、hybrid
    stockfu/scoring/history.py            market/industry/self 状态
    stockfu/scoring/checkpoint.py         保存、恢复、checksum
    stockfu/factors/raw/                  可复用纯 raw 计算器
    stockfu/strategy/alpha.py             0–100 聚合
    stockfu/strategy/portfolio.py         组合政策接口
    stockfu/strategy/risk.py              风险政策接口
    stockfu/backtest/v2_engine.py         批量逐日编排
    stockfu/backtest/v2_manifest.py       运行清单和 artifact
    configs/factor_profiles/              不可变因子档案
    configs/alphas/                       alpha 定义
    configs/portfolio_policies/           组合定义
    configs/risk_policies/                风险定义

现有文件建议：

| 当前对象 | 动作 |
|---|---|
| stockfu/services/factors.py、valuation.py、dividend.py | 保留点时数据接口；拆出纯 raw，增加 available_at/source_max_date |
| stockfu/ai/operators/factors/*.py | 提取合格 raw 后删除 V1 score 算子；不可原地混用两种契约 |
| stockfu/ai/operators/base.py | V2 不复用 OpResult.score；待调用迁移完成后删除或仅留旧归档工具 |
| stockfu/ai/operators/aggregators/weighted_sum.py | 用 V2 alpha 加权平均替代，随后删除旧实现 |
| stockfu/ai/operators/runner.py | 回测不再逐 code analyze；实时调用迁移到共享批量评分服务后删除旧编排 |
| stockfu/ai/operator_cache.py | 重建为 raw cache；旧 operator_result 只读到归档完成，然后清除 |
| stockfu/ai/action.py | 删除 score_full 映射；保留的交易动作必须接收目标权重而非 raw score |
| stockfu/ai/rebalancers/* | 保留算法思想；改为接收 strategy_score 和独立 policy，逐项测试后决定原地改或重写 |
| stockfu/backtest/engine.py | 提取可复用记账/撮合，V2 编排另建；新旧对照完成后删除旧评分路径 |
| stockfu/backtest/factor_diag.py | 改读 raw/factor score audit，不再直接编译 V1 算子 |
| stockfu/services/signal_scan.py | 直接消费 strategy_score；删除二次 0–100 映射 |
| stockfu/services/recommend.py、evaluator.py、邮件/API | 最后迁移展示和选取逻辑；不参与分数计算 |
| stockfu/ai/strategies/*.yaml | 完整归档后全部删除，按三类 V2 配置重建最小集合 |
| stockfu/backtest/full_cycle_update.py | 改为 deployment catalog；旧 catalog 写入 runtime-bindings 后删除 |

## 21. 分阶段实施计划与门禁

### 阶段 0：冻结并归档 V1

1. 禁止继续新增 V1 因子或策略。
2. 编写只读归档工具，展开 52 个 YAML、variants、默认值和 runtime binding。
3. 生成第 17 节全部文件、checksum 和 52/52 覆盖报告。
4. 不删除业务文件，先提交独立归档 PR。

完成标准：任一旧 id 都能定位到完整配置、运行绑定、历史结果或明确缺失原因。

### 阶段 1：建立契约和 profile registry

1. 实现 RawFactorObservation、FactorScoreObservation、StrategyScoreObservation。
2. 实现稳定 canonical JSON fingerprint，字典键排序，浮点和日期规范化。
3. 实现 profile schema 和加载校验。
4. 先用虚构数据完成 fixed/hybrid/缺失/成熟单元测试。

完成标准：尚未接入真实因子，也能证明 0–100、单调、线性插值和版本不可变。

### 阶段 2：历史状态引擎

1. 实现 self exact rolling 状态。
2. 实现按采样日分组、可精确逐出旧截面的 market/industry 状态。
3. 实现中秩 ECDF、快照统计和 checkpoint。
4. 用乱序资产输入验证结果不变，用中断恢复验证逐位一致。

完成标准：state cutoff 永远小于评分日，prefix invariance 通过。

### 阶段 3：迁移原始因子

按低风险到高风险顺序：

1. fixed 因子：Beta、布林位置、Donchian、RSI、signed R2、TS momentum。
2. 价格历史混合因子：momentum/reversal、bias、volatility、overnight/intraday、volume drought。
3. 横截面与慢变量：股息率、E/P、B/P、规模、换手、Amihud。
4. 重写离散或口径错误因子：MACD strength、continuous trend strength、residual reversal。

每迁移一个 raw_metric 都要做：点时边界测试、旧 raw 对照、缺失测试、单位测试、profile 分布报告。不能一次性批量改 33 个后再排错。

### 阶段 4：批量评分与 alpha

1. 实现每天一次 batch raw -> factor score。
2. 实现 alpha 加权平均、coverage 计算和关键因子门禁。
3. 证明同一 profile 在不同 alpha 中分数相同。
4. 输出每日全量 audit，不接仓位。

完成标准：选定若干日期人工复算 PE/PB/股息/动量，误差在浮点容差内；没有 score_full。

### 阶段 5：V2 回测编排

1. 接入点时 universe 和现有撮合记账。
2. 实现 1/5 观察期、成熟延期、t+1 执行和 state 日末更新。
3. 先跑 no-trade 和恒定 50 的哨兵策略，再跑单因子。
4. 输出 manifest、observation/formal 分区和 checkpoint。

完成标准：观察期零订单；前缀一致；重跑一致；延长结束日不改历史。

### 阶段 6：组合与风险政策

1. 迁移 cap/rank、top N、equal/risk weight 为独立 portfolio policy。
2. 迁移止损、止盈、刹车、regime 为独立 risk policy。
3. 对相同 alpha 的政策变体检查 strategy_score 完全一致。
4. 所有风险规则采用 t 时点已知净值和价格，写独立无未来测试。

完成标准：alpha、policy、risk 三份 fingerprint 独立；任何政策修改不重算 alpha score。

### 阶段 7：实时荐股、邮件和 API

1. 实时服务从 canonical checkpoint 续算，禁止另建一套分数公式。
2. signal_scan 直接读取 strategy_score、coverage、maturity 和原因。
3. 邮件/API 展示 0–100、关键因子分、原始值、历史截止和不可交易原因。
4. 回测日重放与实时服务对同一 data cutoff 输出必须一致。

### 阶段 8：删除 V1

1. 确认归档门禁、V2 回归和使用方迁移全部通过。
2. 删除第 17.4 节对象和旧缓存。
3. rg 全库确认 score_full、旧 raw_score remap、旧 strategy id 只存在 docs/legacy 和 migration-map。
4. 更新 docs/WORKSTATE、BACKTEST、CLI 帮助和数据库迁移说明。

## 22. 测试矩阵

### 22.1 映射单元测试

- 每个 profile 对端点、中点、区间内四分点、越界和 NaN 测试。
- 固定映射在相邻锚点间严格等比例；给定 x1、x2、x_mid，score_mid 等于两端均值。
- higher/lower 方向单调正确。
- ECDF 相同值用中秩，与资产输入顺序无关。
- hybrid 权重和、成熟收缩、分量缺失和 evidence_coverage 正确。
- 所有 score 是有限数且处于闭区间 [0,100]。

### 22.2 点时与无未来测试

- 数据源最大日期断言。
- 公告日期、除息日、实施日的边界前后测试。
- 股票上市、退市、指数纳入剔除和行业变更边界测试。
- t 日先 score 后 update 的 spy/state 测试。
- prefix invariance 和 end-date extension 测试。
- 当前截面股票顺序随机打乱测试。
- history_origin 变化必须改变 run fingerprint。

### 22.3 回测协议测试

- N 为 1、4、5、6、非整除 5 时 observation_count=ceil(0.2N)。
- 观察期订单、成交、持仓均为空。
- 首个 formal scoring 日与首个实际成交日正确。
- 成熟不足时 no-trade 延长且记录天数。
- checkpoint 前后连续运行和分段恢复结果逐位一致。
- 同 alpha 不同 policy/risk 的每日策略分完全相同。
- 费用、滑点、停牌、涨跌停和现金约束不修改 strategy_score。

### 22.4 数据质量和统计验收

每个 profile 在观察期和正式期分别输出：raw count、missing rate、unique ratio、min/max、P01/P05/P25/P50/P75/P95/P99、factor score 同组统计、0/100 比例、clamp rate、市场/行业/self 样本数。

候选警戒线而非强制优化目标：

- 非稀有事件因子 formal missing rate 超过 5% 时不得 active，除非有书面例外。
- 连续因子 0 或 100 单端比例超过 10% 时检查锚点，不能为了通过而偷偷用当日 rank。
- 连续因子 unique score ratio 低于 10% 时检查离散化。
- industry component 覆盖不足时退化路径必须可见。
- 任何股票 coverage 低于策略门槛都不得新开仓。

### 22.5 人工金样本

至少固定 10 只股票，覆盖银行、周期、消费、科技、亏损、负净资产、高股息、零股息、新股和退市股；选择 2008、2015、2020、2024、2026 的关键日期。手工保存原始输入、计算过程和期望分数。金样本必须包含 PE<0、PB<0、股息公告边界、除息日、停牌和行业缺失。

## 23. 验收标准

系统只有同时满足以下条件才算改造完成：

1. 所有 active 因子档案输出 0–100，方向统一，50 有文档依据。
2. 同一股票、日期、profile 在任何策略中分数一致。
3. 每个 active alpha 为每个点时合格股票输出数值、coverage 和 status；缺失不再伪装成 0。
4. 横截面比较不依赖股票遍历顺序，纵向历史不被未来或结束日改写。
5. 首 1/5 观察期不交易，后 4/5 持续更新历史状态。
6. 所有回测可由 manifest 和 checkpoint 确定性复现。
7. PE 与 PB 已拆分；亏损和负净资产有明确语义；graham/value 旧复合因子不存在于 active registry。
8. alpha、portfolio、risk 完全解耦，相同 alpha 的政策变体得分相同。
9. 邮件、API、回测和实时荐股共用同一评分服务，不存在展示层再映射。
10. 52 个 V1 策略已完整归档，活动目录和运行 catalog 中不存在不可复用旧配置。
11. rg 全库只允许 docs/legacy 中出现 score_full 和旧策略复现内容，业务代码为零。
12. 完整测试、金样本、前缀一致、恢复一致和数据质量报告通过。

## 24. 给执行模型的逐步清单

后续模型必须按以下顺序工作，每一步单独提交，禁止跳步大改：

1. 先阅读本文件、AGENTS.md、WORKSTATE、现有 BACKTEST 文档和所有待改模块；记录工作树，不覆盖用户改动。
2. 只读生成 V1 inventory，断言因子 33 个、基础 YAML 52 个；实现归档但不删除。
3. 展开 variants 和所有运行时默认，生成 archive coverage test；人工审阅 unknown 字段。
4. 建 V2 contracts 和 schema，不接真实数据库；完成映射单测。
5. 建 history state 和 checkpoint，用合成数据完成无未来、顺序无关和恢复测试。
6. 先迁移一个 fixed 因子 low_beta、一个 hybrid 因子 dividend_yield、一个拆分因子 earnings_yield，打通端到端 vertical slice。
7. 实现单 alpha 只评分、不交易的批量日循环；检查 reference_cutoff。
8. 接 1/5 observation 和空订单哨兵；先验证日期边界。
9. 接撮合记账，信号日和成交日分离；跑极短金样本。
10. 按第 21.3 节顺序逐因子迁移，每个因子一个分布报告和一组测试。
11. 接 alpha coverage 和关键因子门禁，证明跨策略 profile 一致。
12. 接 portfolio/risk，证明政策不改变分数。
13. 运行 canonical 全周期回测；保存 manifest、checkpoint、observation/formal 结果。
14. 迁移 signal_scan、recommend、evaluator、邮件和 API，删除所有二次映射。
15. 对照 V1 只解释差异，不要求收益或订单一致，因为量纲已经有意改变。
16. 确认 V1 archive 门禁和全部 V2 验收后，再删除旧 YAML、旧 score 代码和缓存。
17. 最后执行 rg、全测试、数据迁移 dry-run、文档校验，更新 WORKSTATE 并提交 PR。

每一步如果发现当前代码与本文不一致，应先更新设计决定或提出问题，不能自行发明兼容规则。尤其禁止为了让旧回测曲线相似而修改 0–100 语义。

## 25. 需要在实施前用数据确认、但不阻塞架构的参数

以下是参数验证，不是架构未决：

- 各候选 fixed knots 是否造成过高 clamp rate。
- 快因子市场历史采用周末还是每五个交易日采样。
- 各行业历史 min_observations 是否对早期小行业可达。
- size 使用总市值还是自由流通市值作为主 profile；二者应是不同 raw_metric/profile，不能混用。
- 财务数据 available_at 能否严格恢复历史公告时间；不满足时相关因子不得 active。
- 首版正式策略选择 weekly 还是 monthly rebalance。
- 旧策略历史结果中哪些值得作为 V2 家族的对照，不影响删除旧活跃配置。

默认建议已经写在第 8、11、12 节。调整这些参数必须形成 profile 或 policy 新版本，并重新跑 canonical 全周期回测。

## 26. 最终原则

0–100 是一份长期契约，不是一层漂亮的 UI。原始值说明“发生了什么”，因子分说明“对该因子有多好”，策略分说明“对该 alpha 有多合适”，仓位说明“愿意承担多少风险”。四者任何两层再次混在一起，都会回到当前系统的问题。

V2 允许历史参考随时间吸收新事实，但不允许未来事实改写过去；允许不同因子使用不同、可调窗口，但不允许策略私自改变同一因子的含义；允许删掉不可复用的旧实现，但必须先留下足以复现其真实行为的完整记录。
