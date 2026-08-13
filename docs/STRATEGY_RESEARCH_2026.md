# 网络流行策略调研与本地实现 · 2026-08

> 本文记录策略筛选、数据边界和实现决策。回测数字与验收结论只维护在
> [`docs/BACKTEST.md`](BACKTEST.md)，策略参数以 `stockfu/ai/strategies/*.yaml`
> 和对应算子源码为准，避免同一结果在多份历史报告中漂移。
>
> 调研日期：2026-08-02；本页状态更新：2026-08-13。三策略调优的详细流程和最终数字以 [`v2-tuning-results.md`](SPECS/v2-tuning-results.md) 为准。
>
> 2026-08-13 补充：风格因子最新实证调研（质量/成长/盈利、新异象、行业轮动）见 [`SPECS/style-factor-research-2026.md`](SPECS/style-factor-research-2026.md)，其中质量/盈利与价值因子负相关互补、隔夜-日内分解为日线可复现的新维度。

## 1. 调研方法

筛选顺序是“先确认数据，再判断策略”：

1. 盘点 `quote_snapshot`、`dividend_event`、`index_*` 和情绪/资金流表的字段与历史范围。
2. 对照 A 股 2025–2026 因子综述、经典论文和 A 股异象实证，排除无法忠实实现的策略。
3. 优先选择与现有策略正交、可由当前日线数据复现、且能通过三跑门禁的候选。

网络资料用于提出假设，不等于本地结果已经证明该假设。新增策略必须遵守
[`BACKTEST.md`](BACKTEST.md) §0.6.6 的全样本 + 两段子样本门禁。

## 2. 数据能力边界

| 因子类别 | 当前状态 | 来源或限制 |
|---|---|---|
| 量价（OHLCV、amount、turnover、pct_chg、三套复权） | ✅ | `quote_snapshot` 全市场日线 |
| 价值（PE-TTM、PB-MRQ） | ✅ | `quote_snapshot.pe/pb`，分位运行时计算 |
| 股息（TTM 现金分红） | ✅ | `dividend_event` |
| 规模（总市值） | ⚠️ | `market_cap` 列存在但当前全库为空，`size` 使用 amount×100/turnover 代理 |
| 流动性（换手、Amihud） | ✅ | `turnover`、`amount` |
| 相对市场 β | ✅ | 个股与 `sh000300` 同日期对齐 |
| 历史指数成分 | ✅ | 沪深300、中证500时点成分并集 |
| 情绪与资金流 | ✅（择时/展示） | `index_snapshot`、`factor_snapshot`、`sector_flow_snapshot` |
| 质量（ROE、ROA、毛利率） | ❌ | 未接入财务三表 |
| 成长（利润或营收增速） | ❌ | 未接入利润表时序 |
| PS、EV/EBITDA、流通市值 | ❌ | 当前模型没有对应 PIT 字段 |

因此本批的低波、低 β、低回撤和趋势平稳只能作为“质量”价格代理，不能宣传为真实质量因子。
`size` 的代理期也必须在报告中注明，直到补齐 `mktcap` 并重做历史回补。

## 3. canonical 策略集合

当前第一批 canonical 集合是 9 个主题策略加 1 个 Smart Beta 复合参照，共 10 个；
`momentum_acceleration` 是支持时序动量的算子，不单独构成策略。

| 策略 ID | 主要暴露 | 本地可实现数据 | 设计目的 |
|---|---|---|---|
| `fifty_two_week_high_cross_section` | 52 周新高 + 动量 | qfq close | George–Hwang 近高点效应 |
| `small_cap_low_turnover` | 小市值 + 低换手 | 市值代理、turnover | 规模异象并过滤投机换手 |
| `low_turnover_reversal` | 低换手 + 反转 + 价值 | turnover、close、PE/PB | 捕捉被错杀的低流动性稳健票 |
| `illiquidity_value` | Amihud 非流动性 + 价值 | amount、close、PE/PB | 非流动溢价与低估复合 |
| `anti_lottery_defensive` | 反 MAX + 低波 + 价值 | close、PE/PB | 避开高彩票特征股票 |
| `low_beta_dividend` | 低 β + 红利 + 价值 | close、沪深300、股息 | 防御收入暴露 |
| `ts_momentum_trend` | 时序动量 + 加速 + 趋势 | qfq close | 波动调整后的趋势跟踪 |
| `graham_defensive_value` | PE/PB 分位 + 股息 + 低波 | PE/PB、股息、close | 防御型价值与安全边际 |
| `donchian_breakout_cross_section` | 唐奇安通道 + 时序动量 | qfq close | 日线通道突破 |
| `smart_beta_multi_factor` | 52 周高、低波、价值、低换手、Graham | 上述字段 | 分散暴露参照，不宣称已证明 alpha |

策略 YAML 的共同底线是 `max_gross=1.0`、单票 `max_w=0.20` 和显式风险配置；来源没有止损的策略关闭引擎默认止损，只有 Graham/Donchian/TSMOM 保留各自映射的特殊机制。完整配置见 [`strategy_specs/NEW_STRATEGIES_2026.md`](../strategy_specs/NEW_STRATEGIES_2026.md)。

## 4. 算子实现要点

10 个新算子均通过 `@register` 自注册，由 `discover_and_register()` 自动发现，遵守
`BaseOperator.run(ctx, params) -> OpResult`：`score` 用于聚合，`value` 保存原始值，`signal`
仅用于展示。

- `fifty_two_week_high`、`donchian_breakout`：用 qfq 收盘价计算通道位置。
- `ts_momentum`、`momentum_acceleration`：收益按波动率归一，并把大窗口的日历日缓冲放大到
  `int(window*1.5)+30`，避免交易日样本不足。
- `low_beta`：股票和 `sh000300` 按日期交集计算，不用不同长度序列的末段硬截断。
- `size`：优先读 `market_cap`，为空时使用 20 日平均的 `amount×100/turnover` 代理，并降低 confidence。
- `low_turnover`、`illiquidity`、`lottery_max`：先生成连续分数，再交给 `cap_and_rank` 做横截面竞争，避免绝对阈值直接决定持仓。
- `graham_value`：复用估值分位和 TTM 股息 provider，不增加回测逐票查库。

回测引擎已把 `amount`、`market_cap` 和 `turnover` 纳入列式预载；算子在回测热路径读取内存切片，避免因子逐票 N+1 查询。低 β 的日期对齐和预载窗口是本批实现中已单独验证的正确性边界。

## 5. 当前验证状态

- 10 个算子已注册，10 个 canonical YAML 可编译并已登记到 seed 目录。
- `small_cap_low_turnover` 已完成真实数据端到端冒烟，覆盖市值/换手预载和市值代理路径。
- 第一批 10 个 canonical full 已完成；统一结果表、口径和风险审计见 [`BACKTEST.md`](BACKTEST.md) §0.6.10。
- canonical full 中 7/10 的总收益高于沪深300，但这不是样本外通过结论；整体十策略的修正风险配置 train/test 尚未全部重跑，不能把该预筛选结论外推为十策略整体的样本外结论。
- `smart_beta_multi_factor` 仅作风格暴露参照：历史持仓显示明显小盘倾斜，正式保留前仍需市值中性化和三跑验证；排查记录见 [`BACKTEST.md`](BACKTEST.md) §0.6.8。
- 在上述预筛选基础上，价值、高股息、多因子三套已完成执行层、Alpha 层、风险覆盖层三阶段调优，并完成统一日调仓的三段 canonical 复核；最终结论见 [`v2-tuning-results.md`](SPECS/v2-tuning-results.md)。

### 第二批研究模板（非 canonical 结论）

以下 11 个 YAML 是后续研究候选，当前不属于第一批 canonical 集合，也没有可用于正式保留判断的三跑结果。它们保留在代码库是为了继续做可行性验证；不应把日线代理写成论文策略的完整复现。

| 策略 ID | 当前日线映射 | 主要缺口 |
|---|---|---|
| `amplitude` | 20 日 `(high-low)/close` 分位 | 无日内路径 |
| `bias_reversal` | 收盘价相对均线偏离分位 | 未做论文持有期/行业中性 |
| `intraday_return` | 20 日 `close/open-1` 均值 | 无分钟数据，公开结论方向不稳 |
| `limit_up_count` | 60 日涨停计数 | 各板块涨跌停制度被统一阈值近似 |
| `low_downside_vol` | 60 日负收益半方差 | 只有横截面排序，无前瞻目标收益 |
| `low_skewness` | 60 日收益偏度 | “低偏度=低彩票”在 A 股仍需验证 |
| `near_52w_low` | 距 252 日低点距离 | 未做事件、行业和规模中性 |
| `overnight_reversal` | 20 日隔夜收益均值 | 文献方向冲突，当前只是候选假设 |
| `residual_reversal` | 相对沪深300的日线残差 | 缺少原研究的日内因子模型 |
| `rsi_reversal` | 14 日 RSI 历史分位 | 参数是候选默认值，不是论文复现 |
| `volume_drought` | 5/120 日成交额均值比 | 缺少盘口冲击和成交结构 |

旧的探索性 full 产物和组合刹车配置审计前的数字不再作为结论；如果这些候选进入保留流程，必须按当前配置重新跑全样本和两段子样本。回购事件策略、Level-2/分钟策略目前仅记录数据缺口，不用日线代理冒充原策略。

## 6. 后续事项

1. 在 `stockfu/data/baostock_source.py` 加入 `mktcap` 字段解析、DTO 和落库，完成历史回补后重新验证 `size`。
2. 若接入财务三表，新增真正的质量/成长算子，并把当前价格代理降级为独立风格因子。
3. 价值、高股息、多因子三套的修正配置三段复核已完成；其余第一批候选仍需在进入正式保留集前按当前协议重跑。
4. 让 `low_beta` 的基准可配置，评估沪深500对中小盘策略的适配性。
5. 考虑在 YAML 缺少风险段时发出显式告警，防止静默继承引擎默认止损。

## 7. 参考来源

- [George & Hwang (2004), The 52-Week High and Momentum Investing](https://www.bauer.uh.edu/tgeorge/papers/gh4-paper.pdf)
- [Moskowitz, Ooi & Pedersen (2012), Time Series Momentum](https://www.aqr.com/Insights/Research/Journal-Article/Time-Series-Momentum)
- [Amihud (2002), Illiquidity and stock returns](https://doi.org/10.1016/S0304-405X(01)00065-6)
- [Bali, Cakici & Whitelaw (2011), Maxing Out](https://doi.org/10.1016/j.jfineco.2010.10.014)
- [Frazzini & Pedersen (2014), Betting Against Beta](https://www.aqr.com/Insights/Research/Journal-Article/Betting-Against-Beta)
- [Banz (1981), The relationship between return and market value](https://doi.org/10.2307/2327357)
- [上海财经大学：换手率——流动性还是不确定性](https://qks.sufe.edu.cn/mv_html/j00003/201805/136942e3-4ab3-4dff-a60f-99e23aec1b58_WEB.htm)
- [哈工大《管理科学》：有限套利与特质风险](https://glkx.hit.edu.cn/__local/5/A6/B7/AAD6273C7B5021061358DB55D48_A079D3FD_3DB564.pdf)
- [《金融研究》：市场摩擦对特质风险溢价的影响](http://www.jryj.org.cn/CN/article/downloadArticleFile.do?attachType=PDF&id=924)
