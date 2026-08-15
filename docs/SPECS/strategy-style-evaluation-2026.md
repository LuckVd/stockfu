# 新风格策略方向评估（2026-08-15）

> 目标：为 StockFu 找与现有五套正式荐股（价值/高股息/多因子/质量增强/盈利动量进攻，
> 均为**持续因子暴露型个股选股**）风格不同的策略方向。本文档是 2026-08-15 网络调研
> + 数据实测 + V2 架构走读后的**评估结论**，不是回测结论；任何方向开工前须按
> `docs/BACKTEST.md` §0.6.6 三跑门禁执行，canonical 快照一律用 `bcf8e882afee`。
>
> 评估方式：5 个并行子代理分别走读 `stockfu/scoring/`、`stockfu/strategy/`、
> `stockfu/backtest/v2_engine.py`、`docs/BACKTEST.md` 并实测 SQLite 数据覆盖，
> 另用 web_search 补充实证证据。数据盘点为当日实测。

## 1. 数据盘点摘要（2026-08-15 SQLite 实测）

| 数据 | 覆盖 | 关键字段 |
|---|---|---|
| `quote_snapshot` 全市场日线 | 2013-01→2026-08-14，2001 只 | OHLCV qfq/raw/hfq、pe/pb、turnover；`market_cap` 全库为空 |
| `dividend_event` | 2013-03→2026-08-21，1902 只，19301 笔 | ex_date/record_date/announce_date/per_share_cash；`per_share_cash_after_tax` 仅 20 行非空；5–7 月 ex_date 占 80.6% |
| 财务四表（profit/growth/balance/cashflow） | 各约 25 万行，pub_date 2010-04 起，PIT | net_profit_yoy/revenue_yoy 覆盖 94%（5225 只） |
| `index_quote_daily` | 2013 起，36 个指数 | 含 31 个申万一级 sw801xxx（数据到 2026-07-16，滞后约 1 个月）+ 大盘指数 |
| `sector_snapshot` | 90 行业 2020-01→2026-08-14 | 行业日线量价 |
| `sector_flow_snapshot` | **仅 3 周**（2026-07-24→08-14，92 行业） | 板块资金流只能未来积累 |
| `market_factor_daily` | 2015-07→2026-07-06，2439 行 | margin_balance 2009 行（2023-09 起断档）；breadth 3 行、limit_chain 22 行、limit_count 371 行（2025-06 起）；**表已无代码读写（孤儿表）** |
| `factor_snapshot` | 仅 2026-07-18→08-14 | 三层情绪外生因子历史极短 |
| `index_constituent` | 000300/000905 2012-07 起、000852 2025-04、000688 2020-07 | 回测宇宙 |
| `etf_quote_daily` / `fundflow_snapshot` | 10 只 ETF / 2026-08-05 起 | ETF 宇宙小 |

要点：**行情/股息/财务数据充足且长**；**资金流与情绪外生因子历史极短**；
`index_snapshot` 市场层 fear/greed/heat 主分由基准 K 线派生（`services/composite.py`），
可按 PIT 重建 2013+ 全序列，但那是 regime 代理而非完整外生情绪。

## 2. 五方向评估结论总表

| 方向 | 结论 | 数据 | V2 适配 | 工作量 | 与五套正交性 |
|---|---|---|---|---|---|
| ① PEAD 业绩期事件驱动 | **有条件可行**（宜作第五套事件化增强） | ✅ 财务 pub_date + yoy 齐；❌ 无一致预期/业绩预告表 | 事件型 raw 用「公告后 K 日返回、窗外 None」技巧，不动引擎 | M | 与盈利动量进攻同源净利序列，**重叠大**；与其他四套正交 |
| ② 分红抢权/填权 | **有条件可行**（先 S 级证伪） | ✅ dividend_event 2013+；⚠️ 税字段空、事件季节集中 | 信号可表达；**缺"到期必卖"原语**；须 raw 口径 + credit_dividends=True 扣税 | S 证伪 → M | 信号维度正交；持仓与高股息套重叠 |
| ③ 行业轮动 | **有条件可行**（降级复现） | ✅ sw 指数 2013 起（31 个）；⚠️ sector_snapshot 2020 起过不了 2013–2019 子段；资金流 3 周 | 评分三层天然支持行业=资产；**四硬缺口**：sw 路由/指数 universe/执行口径/档案隔离 | M | 功能正交（配置层 vs 选股层） |
| ④ 情绪择时 overlay | **有条件可行**（大概率是 regime 重参数化） | ⚠️ 外生情绪历史残缺；价格核心可重建 2013+ | `RiskOverlay.apply` 钩子完整（growth-offense 已用同范式） | M | 与五套正交（不改选股）；与既有 market_regime/vol 目标**高度重叠** |
| ⑤ 数据缺口 | 龙虎榜 **值得接入 M**；个股两融 **值得接入 M**；可转债双低 **暂缓 L**；北向 **放弃** | 龙虎榜/两融 akshare 2010+ 可回补、直连；转债转股价历史需重构；北向 2024-08 后仅季披露 | 新表 → 新 raw provider → profile → alpha，路径成熟；转债需资产扩展 | 见左 | 龙虎榜事件驱动正交；两融与盈利动量中正相关 |

## 3. 方向详情

### ① PEAD 业绩期事件驱动（M，有条件可行）

- **形态**：SUE 替代（net_profit_yoy 对自身 4 期滚动均值 z-score / 公告日收益）+ 披露时点
  信号（"优等生早交卷"——早披露行业业绩普遍更好）。
- **数据**：财务表 pub_date 2010+ 全覆盖，net_profit_yoy 覆盖 94%——够。缺分析师一致预期
  （真 SUE 不可得，用时间序列 z-score 替代）与业绩预告表（早交卷载体缺失，退用定期报告
  pub_date，强度打折）。
- **架构**：引擎逐 (metric, code, 交易日) 调 raw 且 `available_at<=as_of` 硬校验已防前视；
  financial.py 字段级 PIT 已落地。SUE-as-level（公告后每日返回最新 SUE）完全符合"每日截面
  持续值"模型；事件型窗口用「公告后 K 交易日内返回 SUE、窗外 None」，缺失日 score 收缩 50
  自动跌出 top-N，等价"公告后 N 日持有"。
- **风险**：强惊喜 T+1 一字板系统性漏买；披露季集中换手；N/K/早晚参数多易过拟合。
- **关键判断**：`earnings_momentum_offense_v2` 已以 growth_accel（同源净利同比序列）为
  critical 主轴，**PEAD 与其重叠大、正交性弱**——更适合作第五套的**事件化增强**
  （披露季叠加 SUE+早晚信号），而非独立全时段持有。
- **证据**：[华泰金工：业绩期价格跳跃中的Alpha信号](https://finance.sina.com.cn/wm/2026-04-27/doc-inhvwwmp2484808.shtml.md)、
  [金融工程：超预期股票精选策略](http://stock.finance.sina.com.cn/stock/go.php/vReport_Show/kind/lastest/rptid/777910105872/index.phtml)

### ② 分红抢权/填权（S 证伪 → M）

- **形态**：已公告且 ex_date∈[t+1, t+W] 的税前股息率/close_raw 为信号，登记日前建仓、
  除息后卖出吃填权。
- **数据**：dividend_event 2013+ 1902 只齐，99.85% 事件 announce_date<ex_date 可严格 PIT。
  缺口：`per_share_cash_after_tax` 几乎全空，税须靠引擎持有期规则；5–7 月 ex_date 占 80.6%，
  样本季节集中、子区间区分度弱。
- **架构冲突**：① T+1 成交 + 须在 record_date 前 ≥2 日触发，窗口精度受损；② 无「到期必卖」
  原语——rebalancer 只有 min_holding_days 软锁，靠分数归零被 top-N 挤出依赖 daily 全量调仓；
  ③ 默认 qfq + credit_dividends=False 不扣税，事件策略**必须 raw 口径 + credit_dividends=True**
  （引擎 FIFO 分级税 20%/10%/0% 已实现），否则回测虚高。
- **风险**：20% 短持税 + 双边佣金 + 印花税大概率吞噬大部分 edge；除息缺口可大于
  股息+税+成本之和；X/W/Y 窗口参数极易过拟合。
- **必做前置（S）**：研究脚本按 PIT 事件算 [record_date−X, ex_date+Y] 窗口收益，扣 20% 税+
  双边成本，先证伪净 alpha 再谈实现。
- **证据**：[国联民生：A股银行分红的抢权与填权](https://stock.finance.sina.com.cn/stock/go.php/vReport_Show/kind/lastest/rptid/836123437088/index.phtml)、
  [东方财富：短持需缴较高红利税](https://finance.eastmoney.com/a/202409063177133190.html)

### ③ 行业轮动（M，有条件可行，降级复现）

- **形态**：申万一级行业「长期反转-中期动量-低拥挤」（拥挤用换手/波动代理）。
- **数据**：sw801xxx 31 个指数 2013-01→2026-07-16（滞后约 1 个月，canonical 前需补数），
  是三跑门禁唯一标的；sector_snapshot 2020 起**过不了 2013–2019 子段**，只能当展示/未来
  数据源；sector_flow_snapshot 3 周——资金流只能未来积累，历史用换手率/成交额占比/波动率代理。
  注意 31 码是 2021 现行分类，2013–2020 序列有分类回填/幸存偏差。
- **架构**：评分三层与资产类型无关，动量/波动 raw 可复用；四硬缺口：① `quote_model_for`
  不认 "sw" 前缀；② universe/listing/ST 是股票语义需 index-universe 模式；③ 执行层 qfq/
  整百股/涨跌停只对个股成立——要么名义指数记账（不可交易、高估），要么映射行业 ETF
  （仅 10 只、2016 起）；④ 档案须 fixed+market_history 并加 `market_scope=sw_industry` 隔离。
  组合/风险层可复用；荐股层需新输出面（配置层，不是第六张个股榜）。
- **证据**：[中银量化行业轮动系列(九)](http://stock.finance.sina.com.cn/stock/go.php/vReport_Show/kind/lastest/rptid/779883580073/index.phtml)、
  [华泰：量化行业轮动的"崎岖之路"](http://stockfinance.sina.cn/stock/go.php/paper/reportid/826789039336/index.phtml?vt=4&autocallup=no&isfromsina=no)；
  本地 style-factor-research-2026 已收录招商 2025（行业动量 RankIC 9.67%、2022 后衰减需择时
  +拥挤惩罚）。
- **风险**：31 行业×月频≈160 次独立重平衡统计功效低；2021 分类回填防未来函数；成本/税按
  ETF 口径（免印花税）；行业动量 2022 后衰减，三段方向一致性是主要坎。

### ④ 情绪择时 overlay（M，有条件可行，基线对照是关键）

- **形态**：`RiskOverlay.apply`（只改目标敞口不改选股，日级触发，override_locks 豁免软锁）
  加情绪阈值/滞回压仓，叠加在五套之上。与既有 `market_regime`（外生序列+阈值+滞回压仓）
  同范式，换一条输入序列即可。
- **数据真相**：`market_factor_daily` 并非全因子 2015 起——breadth 3 行、limit_chain 22 行、
  limit_count 371 行（2025-06 起）、margin_balance 2015-07→2026-07 但 2023-09 起断档 2.75 年
  （且该表已无代码读写）。`factor_snapshot` 2026-07 起。唯一长历史是**价格派生情绪核心**
  （volatility/momentum/activity 分位，`services/composite.py` 可按 PIT 重建 2013+）——但它
  本质是 trend/vol regime 代理，与既有 market_regime/vol 目标高度重叠。
- **关键判断**：growth-offense 已收敛「任何风控压仓都是收益换回撤的零和交易、vol8 为最优
  平衡点」（`docs/SPECS/growth-offense-gate-results.md`）。overlay 验证标准必须是**风险调整
  后净胜** no_overlay / market_regime_vol4 / vol8 基线，只减回撤不加风险调整收益即不合格。
  外生情绪（两融/连板/广度）真正正交但历史缺失——只能短窗补充证据或攒数年后验证。
- **证据**：[华泰金工情绪面指标测试](https://finance.sina.cn/2021-02-05/detail-ikftpnny5011008.d.html)、
  [申万结构化情绪择时](https://stock.finance.sina.com.cn/stock/view/paper.php?symbol=sh000001&reportid=787162376281)
- 日历效应可同框架并测（春节/月末/财报季），但 A 股证据近年弱化，只作 overlay 不作主策略。

### ⑤ 数据缺口评估（龙虎榜/两融值得接入，转债暂缓，北向放弃）

| 数据 | 源（akshare） | 回补成本 | 实证 | 结论 |
|---|---|---|---|---|
| 龙虎榜 | `stock_lhb_detail_em`/`stock_lhb_stock_detail_em`（含机构专用席位买卖净额） | 2005 起逐日约 4000 请求，直连 | 开源金工：机构席位净买事件后 20–60 日 alpha | **值得接入（M）**：新表 lhb_event → raw 滚动计数 → profile+alpha → 三跑 |
| 个股两融 | `stock_margin_detail_sse/szse`（融资余额/买入/偿还、融券余量） | 2010-03 起沪深各约 4000 请求 | 杠杆资金行为偏中期动量，须流通市值中性化 | **值得接入（M）**：注意 2024-02 转融券暂停后融券侧失真 |
| 可转债双低 | `bond_zh_hs_coupon_daily`+`bond_cov_comparison` | 转债约 800 只；**转股价下修/强赎历史须从公告重构**（最大障碍） | 双低经典但 2024 信用冲击大幅回撤（52 只破面值） | **暂缓（L）**：独立资产类别，V2 执行层（T+1/涨跌停/印花税）不适用转债 T+0/无印花税，需资产扩展；2017 前转债 <30 只，2013–2019 段样本不足 |
| 北向 | 2024-08-19 起改季度披露持股（仅 5%+） | — | 日频数据事实死亡 | **放弃**：只可作 2013–2024-08 历史研究 |

共同缺口：正式回测需含新表的新 canonical 快照（现 bcf8e882afee 无此类表）。
事件类 raw 已有先例（`limit_up_count`），无事件日须明确为有效零值而非缺失
（否则覆盖率违 >5% missing 门槛）。

## 4. 建议排序（2026-08-15）

1. **先做 S 级证伪（本周可完成，不动数据层/引擎）**：抢权/填权窗口收益扣税扣成本研究脚本——
   最快产生"做/不做"决定。
2. **PEAD 事件化增强**（M）：与第五套同源，先做 SUE/早晚信号的 IC 与残差化验证，若与
   growth_accel 正交增量显著再立 alpha，避免第六张榜与第五套重叠。
3. **龙虎榜接入**（M）：数据成本最低、事件驱动风格正交、实证有据；接入后顺带解锁
   "机构行为"因子族。
4. **行业轮动**（M）：需 sw 补数 + `quote_model_for` 路由 + index-universe 模式（引擎改动），
   属配置层，建议与个股五套独立评估、独立输出。
5. **情绪择时 overlay**：先做「价格核心情绪序列 vs market_regime/vol 基线」对照实验，
   跑不出风险调整净胜即判定为既有 regime 重参数化，不再投入。
6. 个股两融（M）排在龙虎榜之后；可转债暂缓；北向放弃。

## 5. 参考来源

- [华泰金工：业绩期价格跳跃中的Alpha信号](https://finance.sina.com.cn/wm/2026-04-27/doc-inhvwwmp2484808.shtml.md)
- [金融工程：超预期股票精选策略](http://stock.finance.sina.com.cn/stock/go.php/vReport_Show/kind/lastest/rptid/777910105872/index.phtml)
- [国联民生：A股银行分红的抢权与填权](https://stock.finance.sina.com.cn/stock/go.php/vReport_Show/kind/lastest/rptid/836123437088/index.phtml)
- [中银量化行业轮动系列(九)：长期反转-中期动量-低拥挤](http://stock.finance.sina.com.cn/stock/go.php/vReport_Show/kind/lastest/rptid/779883580073/index.phtml)
- [华泰：量化行业轮动的"崎岖之路"](http://stockfinance.sina.cn/stock/go.php/paper/reportid/826789039336/index.phtml?vt=4&autocallup=no&isfromsina=no)
- [华泰金工：情绪面指标测试](https://finance.sina.cn/2021-02-05/detail-ikftpnny5011008.d.html)
- [申万：结构化情绪择时](https://stock.finance.sina.com.cn/stock/view/paper.php?symbol=sh000001&reportid=787162376281)
- [东方财富：短持需缴较高红利税](https://finance.eastmoney.com/a/202409063177133190.html)
- [开源金工：机构行为 alpha（龙虎榜/机构调研/大宗）](https://m.jrj.com.cn/madapter/stock/2022/12/29081237245055.shtml)
- [国金：可转债低价及双低策略优化](http://m.hibor.com.cn/wap_detail.aspx?id=45fd14736be6a54806299c875e823ac7)
- [新京报：沪深港通 2024-08-19 起改季度披露持股](http://www.bjnews.com.cn/detail/1724066324129030.html)
- 本地依据：`docs/BACKTEST.md` §0.6.6、`docs/SPECS/style-factor-research-2026.md`、
  `docs/SPECS/growth-offense-gate-results.md`、`docs/SPECS/factor-strategy-score-v2.md`
