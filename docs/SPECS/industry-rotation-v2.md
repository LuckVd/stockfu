# 行业轮动 V2 正式化（2026-08-15 启动）

> 目标：把 `stockfu/backtest/probes/sector_rotation.py`（Phase 1 独立模拟器）升级为
> V2 四层正式链路（alpha/profile/portfolio/engine），在申万一级行业指数上跑
> 「中期动量 + 长期反转 + 低拥挤」三因子轮动，过三段门禁后与五套个股策略衔接。
> 本文档记录管线改动与每轮迭代结果；回测数字只维护在此与 `docs/BACKTEST.md`。
> 背景与五方向评估见 `docs/SPECS/strategy-style-evaluation-2026.md` 方向③。

## 1. 数据事实（2026-08-15 SQLite 实测）

- `index_quote_daily` 含 31 个申万一级指数 sw801xxx，2013-01 → **2026-07-16**
  （比个股行情滞后约 1 个月，canonical 前需补数；数据末日 7-16 前可跑）。
- 31 码是 2021 现行分类，2013–2020 序列有分类回填/幸存偏差 → 文档声明，防未来函数
  靠 V2 硬 PIT 校验不覆盖分类变更。
- `sector_flow_snapshot`（板块资金流）仅 2026-07-24 → 08-14（3 周）：资金流维度
  只能未来积累；历史拥挤度先用波动率代理。
- `index_quote_daily.amount` 填充率 92%（sw801010 全填充）→ 预载已启用真实 amount，
  未来可加成交额维度（成交额占比需截面，raw 层不可行；可用自窗口 z-score/分位）。

## 2. 管线改动（2026-08-15，已提交前状态）

| 文件 | 改动 |
|---|---|
| `stockfu/services/factors.py` | `quote_model_for` 加 sw 前缀 → IndexQuoteDaily |
| `stockfu/backtest/engine.py` | `_preload_market_range` 指数列集启用真实 amount；`VirtualAccount` 加 `fractional_codes`（指数分数仓，买卖含 FIFO lots/费用） |
| `stockfu/services/universe.py` | `board_of_code` sw→"index"、`limit_pct_for("index")=999`（指数无涨跌停）；`UniverseContext.load` 首根 K 按 `quote_model_for` 分表 |
| `stockfu/backtest/v2_engine.py` | acct 构造传 `fractional_codes`（cfg.codes 中 sw 前缀） |
| `stockfu/backtest/v2_run.py` | `DEFAULT_V2_DEPLOYMENTS` 注册 `industry_rotation_v2`（pf_monthly_top8_sw_v2 + no_overlay_v1） |
| `stockfu/factors/raw/momentum.py` | `compute_momentum` 加 `metric_id` 参数（同函数多窗口拆 metric；指纹含 metric_id） |
| `stockfu/factors/raw/volatility.py` | 日历日缓冲 `window+12` → `int(window*1.5)+30`（window=60 时原缓冲取不满 window+1 根） |

新配置：
- `configs/alphas/industry_rotation_v2.yaml`：momentum_60d 0.4 + momentum_250d_rev 0.3
  + volatility_60d_low 0.3；`market_scope: sw_industry`（与个股池评分历史隔离）
- `configs/factor_profiles/momentum_60d_v2.yaml` / `momentum_250d_rev_v2.yaml` /
  `volatility_60d_low_v2.yaml`：均无 industry_history 分量（避免行业自循环）
- `configs/portfolio_policies/pf_monthly_top8_sw_v2.yaml`：月度 top8 等权

## 3. 首版冒烟结果（2026-08-15）

运行：`--backtest-v2 industry_rotation_v2 --start 2021-01-04 --end 2026-07-16
--codes <31 sw> --snapshot stockfu-bcf8e882afee.db`，run_id `bb552892eb7f1014`。

| 指标 | 值 |
|---|---|
| 总收益 / 年化 | -8.76% / -2.13% |
| 最大回撤 / Sharpe | 20.58% / -0.19 |
| 基准（sh000300） | +2.14%（超额 -10.9%） |
| 成交 | 366 笔（月度调仓 top8，5.5 年） |
| raw missing_rate | 三因子均 0.0064（仅预热期） |
| formal coverage | 0.9846 |
| §15 诊断 | p50=53.3、0/100 饱和 0%、唯一值比 99.8%、maturity 正常 |

管线健康（数据完整、诊断正常、分数分布合理），**策略跑输基准**——首版因子
权重/窗口/调仓频率未调，属预期。与 Phase 1 probe（情绪+布林路线）的结果对照
是下一步的第一件事。

## 4. 下一步（按序）

1. **probe 对照**：同窗口跑 `sector_rotation.py` 探测（sw universe、full 情绪模式），
   对比 V2 首版 vs probe 情绪路线 vs 等权基准，判断哪条因子路线有 edge。
2. **sw 指数补数**：找到指数回补入口（`scheduler/jobs.py` 有指数/ETF 回补），
   把 sw801xxx 补到最近交易日。
3. **因子迭代**：窗口扫描（动量 60/120、反转 250/500、波动 20/60）、拥挤度代理
   换成交额分位、调仓频率（月/周）、top-N（6/8/10）——每次改动跑全样本+两子段。
4. **三段 canonical**：三跑门禁（§0.6.6 口径：full 2013+ / 2013-2019 / 2020-2026）。
5. **与五套衔接**：行业轮动是配置层——决策：独立输出 or 作为五套的行业偏离
   overlay（`max_industry_weight` 已预留）。

## 5. 风险备忘

- 名义指数回测不可交易：V2 按指数分数仓记账（无整百股、无涨跌停），成交价用
  指数点位——是"行业配置信号"口径，不是 ETF 可执行口径；如需可交易结论，
  后续可映射行业 ETF（仅 10 只、2016 起）做对照。
- 指数印花税/佣金沿用股票费率（probe 注释同款保守近似）。
- 2021 分类回填 + 新设板块缺席 → 幸存偏差，偏乐观；结论前须声明。

## 6. 第 2 轮（2026-08-15）：probe 对照 + sw 补数 + 情绪路线移植

### 6.1 sw 指数补数完成（`main.py --backfill-sw`）

akshare `index_hist_sw` 回补 31 个行业全历史，写入 `index_quote_daily`（checkpoint 断点
续传 + `upsert_index_daily` cap 硬保证）。补后覆盖：**16 行业 1999-12 起、12 行业
2014-02 起、3 行业（环保/石油石化/美容护理，2021 新分类）2021 起，全部到 2026-08-14**
（与个股行情同步）。→ 三跑门禁全样本段可拉长到 2014+（29 行业）或 2013+（31 行业
含 2014-02 上市的 12 个，universe 首根 K 锚点自动处理）。

### 6.2 情绪+布林路线移植与证伪（重要负面结果）

把 probe 的 panic=high 路线（高恐 + 低贪 + 周布林贴下轨）移植为 V2 alpha
`industry_rotation_emotion_v2`（新 raw：`sector_fear`/`sector_greed`/`sector_heat`
对齐 composite 分位口径 + `bollinger_pctb` 周布林 %b；`market_scope=sw_industry` 隔离）。

同窗口对照（2021-01-04 → 2026-07-16，31 行业，基准 sh000300 +2.14%）：

| 路线 | 总收益 | 年化 | Sharpe | 回撤 | 成交 |
|---|---|---|---|---|---|
| probe 情绪+布林（收盘成交） | +24.09% | +4.14% | 0.50 | 20.75% | 834 笔 |
| V2 情绪版（T+1 开盘，周 top8，min 60） | **-8.47%** | -2.06% | -0.17 | 17.66% | 1757 笔 |
| V2 情绪版收紧（周 top4，min 75） | **-14.44%** | -3.60% | -0.24 | 26.03% | 701 笔 |
| V2 三因子首版（月度 top8） | -8.76% | -2.13% | -0.19 | 20.58% | 366 笔 |

**结论：probe 的 +24% 是「当日收盘成交」保真度简化造成的假象**——恐慌信号在收盘
产生、T+1 开盘执行时跳空已吃掉 edge；收紧持仓（top4/min75，近似 probe 均仓 1.66 只）
反而更差。情绪+布林路线在 T+1 可执行口径下**无 edge**，不做正式保留候选。
V2 情绪版 run_id：`703b2551815137a3`（top8）、`4fb04982242fe848`（top4）。

### 6.3 三因子路线全段验证（完成）

sw 补数后跑全段（2014-01-01 → 2026-07-16，formal 2437 日，基准 sh000300 +46.61%）：

| 指标 | 值 |
|---|---|
| 总收益 / 年化 | -10.44% / -1.13% |
| Sharpe / 回撤 | -0.06 / 27.5% |
| 超额 | -57.05% |

run_id `17fa8d93a62b2faa`。三因子（中期动量+长期反转+低波，月度 top8 等权）
全段无 edge。

### 6.4 行业等权基准对照（判别"行业层 vs 轮动因子"）

31 行业等权（月度再平衡、无成本，指数价格口径）vs 沪深300：

| 段 | 行业等权 | 沪深300 |
|---|---|---|
| 2014-2019 | +66.9% | +76.4% |
| 2020-2026 | -14.6% | +13.2% |
| 全段 2014-2026 | +42.6%（年化 2.98%） | +102.4% |

**行业配置层本身在 2014-2026 就是负超额暴露**（龙头集中行情下行业分散吃亏，
2020-2026 尤甚）；轮动因子（三因子 -10.44%、情绪版 -8.47%）再叠一层损耗。

## 7. 第 2 轮结论（2026-08-15）：方向关闭（暂缓）

**行业轮动方向在 V2 严格口径下无 edge，不做正式保留候选，不进 canonical 门禁**
（§0.6.6 认定门禁只服务有 edge 的候选；负面结论无需三跑）。

- 情绪+布林路线：probe 的 +24% 系"当日收盘成交"假象，T+1 开盘口径下 -8.47%（top8）
  与 -14.44%（top4 收紧）——证伪。
- 三因子路线：全段 -10.44%、超额 -57%——证伪。
- 行业等权跑输沪深300（两子段均负）——行业层在样本期是负暴露，方向期望值为负。

**保留的资产**（管线价值，不随方向关闭废弃）：
- V2 引擎指数资产全链路：sw 路由、指数预载真实 amount、分数仓执行、index 无涨跌停、
  universe 分表首根 K——未来 ETF 轮动/指数增强/资产配置策略可复用。
- `backfill_sw_index`：sw 指数全历史已入库（16 行业 1999 起，到 2026-08-14）。
- 新 raw（`sector_fear/greed/heat`、`bollinger_pctb`）保留在注册表，作研究因子备查。

**如日后重启**（新数据/新因子到位时）：
1. 资金流拥挤度维度（`sector_flow_snapshot` 需先积累 ≥2 年历史）；
2. ETF 可执行口径（行业 ETF 仅 10 只、2016 起，样本受限）；
3. 用沪深300 成分内行业做中性化（剥离行业层负暴露后再看轮动 alpha）。
