# 网络流行策略调研与本地实现 · 2026-08

> 目标:调研当下网络上流行、且**本项目现有数据足以回测**的量化策略,挑选约 10 个,本地实现算子 +
> 策略 + 文档。本文是调研与实现的单一权威记录;算子/策略配置细节以对应的
> `stockfu/ai/operators/factors/*.py` 与 `stockfu/ai/strategies/*.yaml` 为准。
>
> 调研日期:2026-08-02。所有回测结论须先过 [`docs/BACKTEST.md`](BACKTEST.md) §0.6 准入门禁,在此之前
> 不得据此判断策略优劣。

---

## 1. 调研方法

1. **先摸数据边界,再筛策略**——「数据是否支持回测」是硬约束,先于「策略是否流行」。盘点
   `stockfu/models.py` 全部表与列(详见 §3),确定哪些因子类别可回测、哪些不可。
2. **网络调研**(2026-08-02,中英双语多源):
   - A 股 2025-2026 因子有效性综述(果仁网、清华 PBCSF、财新、知乎、华创/中信卖方);
   - 学术经典因子原文/综述(George-Hwang、Moskowitz-Ooi-Pedersen、Amihud、Bali-Cakici-Whitelaw、
     Frazzini-Pedersen BAB、Banz 小盘效应、格雷厄姆防御价值);
   - A 股异象实证(上海财大《换手率》、哈工大《管理科学》MAX、郑振龙-孙清泉彩票股、《金融研究》
     流动性)。
3. **选型三准则**:(a) 当下流行/学术扎实;(b) **本项目数据可回测**;(c) 与现有 13 个算子 /
   30 个策略**正交不重复**(空白区优先)。

## 2. 数据能力边界(选型的硬约束)

经盘点 `quote_snapshot` / `dividend_event` / `index_*` / `*_snapshot` 等表:

| 因子类别 | 可回测? | 数据来源 |
|---|---|---|
| 量价(OHLCV、amount、turnover、pct_chg,三套复权) | ✅ | `quote_snapshot` 全市场(baostock backfill) |
| 价值(PE-TTM / PB-MRQ) | ✅ | `quote_snapshot.pe/pb`;分位运行时算(`valuation.py`) |
| 股息(TTM 现金分红) | ✅ | `dividend_event`(`--backfill-dividend`) |
| 规模(总市值) | ⚠️ 部分 | `market_cap` 列存在但**全库空**(见 §5);可用 amount×100/turnover 代理 |
| 流动性(换手/Amihud) | ✅ | `turnover` / `amount` |
| 相对市场 β | ✅ | 个股 vs `sh000300`(`index_quote_daily`) |
| 指数成分股(universe) | ✅ | `index_constituent`(沪深300+中证500 历史并集) |
| 情绪/资金流(三层恐贪、主力净额、连板) | ✅(择时) | `index_snapshot` / `factor_snapshot` / `sector_flow_snapshot` |
| **质量(ROE/ROA/毛利率)** | ❌ | **无财务三表** |
| **成长(净利润/营收增速)** | ❌ | **无利润表时序** |
| PS / EV / 流通市值 | ❌ | 未入库 |

**关键结论**:2025-2026 最火的「**质量因子**」(ROE/盈利稳定性)与「**成长因子**」(利润增速)**无法
原样回测**——库里没有财务三表。本批用**价格代理**(低波/低 β/低回撤/趋势平稳)近似「质量」属性,
并在策略层诚实标注。`market_cap` 虽有列但未回补,`size` 算子用代理兜底(见 §5)。

## 3. 选定的 10 个策略

按「A 股 2025-2026 流行 + 学术扎实 + 数据可回测 + 与现有正交」筛出 10 个,落在现有策略的空白区
(规模 / 流动性 / 行为异象 / 趋势跟踪 / 价值复合):

| # | 策略(主算子) | 学术出处 / 流行依据 | 所需数据 | 为何适合本项目 |
|---|---|---|---|---|
| 1 | **52 周新高**(fifty_two_week_high) | George & Hwang(2004,JF,1270+ 引);中国 1995-2018 实证持续有效(SSRN) | close | 纯价格;预测力强于传统动量;现有 momentum 系已多次证伪,本策略是更优锚 |
| 2 | **小市值**(size) | Banz(1981);A 股「小盘主线 2025-26 延续」(广发/中信) | market_cap/代理 | A 股最强风格之一;现有策略空白区;用总市值代理可立即回测 |
| 3 | **低换手率**(low_turnover) | 上财《换手率:流动性还是不确定性》;华创「中证800 最佳」 | turnover | A 股最强流动性异象之一;与低波/反转同向;现有空白 |
| 4 | **Amihud 非流动性**(illiquidity) | Amihud(2002,JFE);《金融研究》李少育 2021 | amount+close | 经典流动性因子;与换手率相关但独立;现有空白 |
| 5 | **反 MAX 彩票股**(lottery_max) | Bali-Cakici-Whitelaw(2011,JFE);哈工大《管理科学》A 股实证 | close | 行为金融异象(彩票偏好);MAX 高→跑输;现有无行为因子 |
| 6 | **低贝塔**(low_beta) | Black 低波动异象;Frazzini-Pedersen BAB(2014) | close+指数 | 2025 防御风格走强;与 low_volatility 相关但维度不同(系统性风险) |
| 7 | **时序动量**(ts_momentum) | Moskowitz-Ooi-Pedersen(2012,TSMOM) | close | 区别于现有横截面动量;波动归一更稳健;个股动量已证伪,此为风险调整版 |
| 8 | **动量加速**(momentum_acceleration) | 实务二阶动量(动量衍生) | close | 拐点预警/动能确认;与 ts_momentum 互补 |
| 9 | **格雷厄姆价值**(graham_value) | Graham《聪明的投资者》防御型 | PE/PB/股息 | 多维价值复合(比单一 PE 稳健);现有 value 仅单 PE 分位 |
| 10 | **唐奇安突破**(donchian_breakout) | 海龟交易系统(经典趋势跟随) | close | 区别于 momentum_breakout(月布林);纯通道突破 |

> 另含 1 个 **Smart Beta 多因子复合**策略(`smart_beta_multi_factor`)作为分散暴露示范,共 10 个策略 yaml。

### 不选的(及原因)
- **质量(ROE)/ 成长(利润增速)/ F-Score / 神奇公式**:依赖财务三表,数据缺失。
- **PS / EV/EBITDA / 流通市值因子**:对应字段未入库。
- **配对交易/统计套利**:A 股融券约束 + 实现复杂,偏离本批「单边多头因子」定位。
- **行业轮动**:`backtest/probes/sector_rotation.py` 已有探针且历史证伪,不在本批。
- **再加 dividend+low_vol+value 组合**:红利横截面族已有 13+ 变体,避免拥挤。

## 4. 实现说明

### 4.1 算子层(`stockfu/ai/operators/factors/`,10 个新文件)

全部沿用现有 `BaseOperator.run(ctx, params) -> OpResult` 契约:`score` 连续不 clamp(满强度 ±20),
`value` 为原始数值(供 `ctx.factors` 共享 + 调试),`signal` 派生标签(仅展示)。`@register` 自注册,
`discover_and_register()` 自动发现,无需手工登记算子(仅需在 `seed.py` 的 `_OP_NAMES` 加显示名)。

**设计决策**:
- **score 全部对齐 ±20 满强度**,与现有 momentum/reversal/low_volatility 一致,保证 `weighted_sum`
  加权 + `score_full` 满仓映射的可比性。
- **横截面因子**(size/low_turnover/illiquidity/lottery/low_beta)的绝对 score 用启发式锚点(见各算子
  docstring),最终选股由 `cap_and_rank` 的**横截面排序**决定——与 `dividend_yield`(绝对息率映射 +
  横截面排序)同模式。
- **`low_beta` 按日期对齐**(见 4.3):修了一个长度不等导致 β 失真的坑。
- **`size` 代理**(见 §5):market_cap 空时用 amount×100/turnover 兜底。

### 4.2 引擎预载扩展(`stockfu/backtest/engine.py`)

为让 `size`(market_cap)/ `low_turnover`(turnover)/ `illiquidity`(amount)等**基本面点因子**在回测中
零 DB(避免 [Bollinger N+1 教训](../) 那类逐次查库拖慢),扩展列式预载:

- `_QS_FIELD_KEY` 增 `"amount":"amt","market_cap":"mcap","turnover":"turn"` 映射 → `quote_series` 能从
  预载内存切片这些字段;
- `_COL_KEYS` 末尾追加 `"mcap","turn"`(按键名访问,顺序无关;不影响 `_BI_*` 旧 tuple 路径);
- `_preload_market_range` 的 `col_sets` 三表 SELECT 增 `market_cap, turnover`(ETF/指数填 NULL),
  unpacking + fill loop 各加 2 行。

**纯增量、不破坏热路径**:已通过 `ruff check` + 端到端回测(`small_cap_low_turnover` 跑通)验证。
不影响 `_bar_from_cols`(按键名读,不触新列)与 `_BI_*`(独立位置常量)。

### 4.3 调试中修的两个正确性坑

1. **`low_beta` 长度错配**:首版用 `sc[-n:]/bc[-n:]` 末段截断对齐 stock/bench,但当二者序列长度不等
   (末日不同)时,截断段**日期不对齐** → 协方差失真 → β 失真(茅台一度算出 0.13)。改用
   `quote_series_dates` 取 `(dates, values)`,按**日期交集**对齐,独立验算 β=0.126 与算子一致。
2. **大窗口日历日缓冲不足**:`ts_momentum`/`momentum_acceleration`/`low_beta` 窗口 120 交易日,
   `quote_series(window+30)` 的日历日缓冲(165 日历日≈112 交易日)拿不满 121 交易日 → 样本不足。
   改 `int(window*1.5)+30`(交易日↔日历日比 ≈1.49)。
3. **`low_turnover` 上溢**:低换手段未钳上界,0.17% 算出 +22.6;加 `min(20, …)`。

## 5. 数据缺口与处理

| 缺口 | 影响 | 处理 |
|---|---|---|
| **`market_cap` 全库空**(baostock 回补字段串 `pctChg,peTTM,pbMRQ,turn` 未含 `mktcap`) | `size` 算子无真值 | ① 算子用 `amount×100/turnover` 派生代理(=总股本×价≈市值,20 日均,`confidence` 降到 0.55);② 后续在 `baostock_source.py` 的 `query_history_k_data_plus` 字段串加 `mktcap` + DTO/落库,重补后自动切真值 |
| 无财务三表(ROE/利润增速) | 质量/成长因子不可回测 | 用价格代理(低波/低 β/趋势平稳)近似;诚实标注非真质量因子 |
| 无流通市值 | 规模因子只能用总市值 | 接受;A 股总/流通市值排序高度相关 |
| `per_share_cash_after_tax` 源端固定扣税近似(非持有期分档) | 股息税精度 | 研究模式接受(见 `docs/BACKTEST.md §0.3`);`graham_value` 股息加分仅用有无+息率,不依赖精确税后 |
| 部分个股近期历史浅(如 600519 仅 ~104 日) | 大窗口算子(ts_momentum 等)样本不足 | 算子已 graceful 降级(score 0、hold);全周期回测用 universe 内深历史成分股 |

## 6. 回测口径与门禁(强制)

- **价格口径**:收益/净值走 **qfq**;股息率分母、PE/PB 绝对值走 **raw** + 税前分红(`docs/BACKTEST.md §0`)。
  三跑门禁统一 `--valuation-basis raw` 对齐 §0.6.x 全族。
- **风控/仓位=原来源映射**(2026-08-02 决定,详见本节与 §7.1 的配置记录):
  9 个纯因子**无止损**(`stop_loss: 0` 关默认 8%)、满仓上限(`max_gross: 1.0`)、单票上限(`max_w: 0.20`)、无刹车;
  `ts_momentum` 波动率目标、`graham` 50% 硬止盈、`donchian` 海龟 2N(0.18)+ATR 追踪止盈。
  不套引擎全局默认(8% 止损曾致三跑全负超额)。
- **防未来函数**:所有取数 `<= as_of`;`low_beta` 基准 `sh000300` 严格同窗口。
- **三跑门禁(防过拟合)**:新增策略认定结论前**必须** ① 全样本 2007–2026 一跑 ② 再取两段不同行情、
  较短段 ≥5 年的子区间各跑;两轮方向一致才认定,否则按 §0.6.4 判过拟合(`docs/BACKTEST.md §0.6.6`)。
- **基准对齐**:红利/风格策略的 alpha 须对齐**全收益同类基准**(价格指数不含分红会误导,见
  memory「红利 alpha 基准」)。
- **流动性容量**:`illiquidity`/`size`(小盘)选出的票成交冲击成本高,实盘容量受限,回测须扣双边滑点。

## 7. 验证记录(2026-08-02)

- ✅ 10 算子全部 `@register` 注册(注册表 13→23),`ruff check` 全绿。
- ✅ 10 策略 yaml 全部编译通过(算子 id / 聚合器均存在),登记进 `_STRATEGIES`(29) + `_RETAINED_STRATEGY_IDS`(31)。
- ✅ `seed_operators_and_strategies()` 入库 54 行(算子+策略 upsert)。
- ✅ 真实数据冒烟测试(`600519`/`000012`/`000028`/`000006` @ 2026-07-24):10 算子均产出合理值
  (茅台 size 代理≈1.5 万亿、000012 MAX 10%/-20、低 β 按日对齐后数学独立验算一致)。
- ✅ 端到端回测集成测试:`small_cap_low_turnover`(触发 market_cap/turnover 预载新列 + size 代理)
  2026-01~07 跑通,无 crash,产出真实指标。
- ✅ **第一批 10 个 canonical full 已完成**（2026-08-05，`data/backtest/run-tune-{strategy}-full.*`）：
  2007-01-04→2026-07-21、`--valuation-basis raw`、统一风险配置审计通过，7/10 总收益超过 HS300。
  旧的 30 轮 train/test 批次是在风险配置审计前运行，见下方“历史预审”，不用于最终保留结论。

### 7.1 历史三跑预审（2026-08-03，30/30，配置审计前，不作最终结论）

> **重要**：本节 30 个结果运行时仍有策略 YAML 省略 `portfolio_brake`，实际加载了引擎默认组合刹车 `portfolio_brake_dd=0.10`；公开来源并未声明该机制。完成风险配置审计后，第一批 10 个策略显式关闭组合刹车并重新跑 canonical full，唯一有效的全周期数字见 §7.1.1。该批 train/test 没有按修正后的配置重跑，因此本节的“通过/证伪”不再作为保留判断。

> 批次 00:40–08:40（`data/backtest/new2026_batch_raw.log`，结果 `data/backtest/run-20260803-*.json.gz`）。
> 窗口：full 2007-01-04→2026-07-21 / train 2007-01-04→2016-12-30 / test 2017-01-04→2026-07-21，基准沪深300（full +129.27% / train +60.13% / test +40.7%），`--valuation-basis raw`。
> 配置声明：全部 `max_gross: 1.0` 总仓上限 + `max_w: 0.20` 单票上限 + 组合刹车意图关闭；实际持仓由框架 `cap_and_rank` 竞争额度，**不保证精确等权或固定 5 只**；9 个纯因子 `stop_loss: 0`；ts_momentum 波动率目标 0.15；graham `hard_profit: 0.50`；donchian `stop_loss: 0.18` + ATR 追踪止盈。实际缺省刹车加载问题见上方警告。

**超额 vs 沪深300 对照（30 轮）**

| 策略 | full | train | test | test 回撤 | 判定 |
|---|---|---|---|---|---|
| low_beta_dividend | +59.6% | +98.6% | **+9.7%** | **26.3%** | ✅ 通过 |
| graham_defensive_value | +14.4% | +65.5% | **+11.2%** | 36.2% | ✅ 通过 |
| smart_beta_multi_factor | +378.5% | +215.1% | **+17.9%** | 40.9% | ✅ 通过(衰减剧烈) |
| fifty_two_week_high_cross_section | −131.6% | −73.2% | −58.2% | 49.9% | ❌ 全输 |
| ts_momentum_trend | −178.6% | −84.6% | −87.9% | 49.1% | ❌ 全输 |
| donchian_breakout_cross_section | −166.0% | −92.8% | −78.5% | 60.2% | ❌ 全输(止损17–20笔) |
| small_cap_low_turnover | +284.3% | +349.7% | −33.1% | 40.6% | ❌ test 崩 |
| low_turnover_reversal | +75.2% | +186.7% | −65.4% | 41.1% | ❌ test 崩 |
| illiquidity_value | +68.5% | +252.2% | −68.1% | 56.7% | ❌ test 崩 |
| anti_lottery_defensive | −53.2% | +93.4% | −51.8% | 48.7% | ❌ 方向不一致 |

**历史预审结论（已被配置审计覆盖）**：

- 历史配置下曾标记 3 个候选，但该标记已撤销；不得把它当作修正配置后的三跑门禁结果。
- **追趋势族全输**（52周高/时序动量/唐奇安）：满仓（max_gross 1.0）+ 无止损（donchian 有 0.18 止损仍输）在 A 股高波动震荡市致命，回撤 49–87%。
- **小盘族过拟合证伪**（小盘/反转/非流动/反彩票）：train 超额 +94~+350%、test 全负——2017 后 A 股小盘壳价值/流动性溢价消退，**与 §0.6.4 hold 证伪同模式，印证三跑门禁价值**。
- **与自己的策略（§0.6.2–0.6.4 base/融合，同窗口同基准）横向对比**：这些比较仍是旧配置历史记录；canonical 结束后 active 指针已恢复为 `graham_defensive_value`。
- **风格 caveat（沿用 §0.6.5）**：low_beta/graham 为防御价值风格，其超额含红利风格 beta + 分红再投成分，真实 alpha 待全收益红利指数（H00922/H30269）验证；smart_beta 含 fifty_two_week_high/low_turnover 暴露，需防小盘风格回归。
- 缓存保留集（3 候选引用算子，勿清）：`low_beta, dividend_yield, value, graham_value, low_volatility, fifty_two_week_high, low_turnover`。

### 7.1.1 canonical full 结果（2026-08-05，10/10，最终全周期记录）

统一口径：2007-01-04→2026-07-21、4749 个交易日、`--valuation-basis raw`、沪深300基准 +129.27%、历史沪深300/中证500时点成分宇宙、`cap_and_rank`、`max_gross=1.0`、单票 `max_w=0.20`、T+1 开盘卖优先。水下天数以**初始本金**为基准，百分比以 4749 个交易日为分母；它不是相对历史峰值的回撤持续期。每个策略的完整原始结果见 `data/backtest/run-tune-{strategy}-full.json.gz` 及对应 `.meta.json`。

| 策略 | 总收益 | 年化 | 超额 | 基准 | 最大回撤 | 回本天数/状态 | 夏普 | 索提诺 | Calmar | 交易笔数 | full 判定 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| `low_beta_dividend` | 719.19% | 11.81% | 589.92% | 129.27% | 55.02% | 453/已回本 | 0.62 | 0.59 | 0.21 | 1304 | ✅ |
| `graham_defensive_value` | 725.49% | 11.85% | 596.22% | 129.27% | 68.65% | 257/已回本 | 0.57 | 0.55 | 0.17 | 1995 | ✅ |
| `smart_beta_multi_factor` | 710.68% | 11.74% | 581.41% | 129.27% | 59.57% | 349/已回本 | 0.56 | 0.55 | 0.20 | 1970 | ✅ |
| `fifty_two_week_high_cross_section` | 66.61% | 2.75% | −62.66% | 129.27% | 74.39% | —/未回本 | 0.23 | 0.22 | 0.04 | 2283 | ❌ |
| `ts_momentum_trend` | −18.57% | −1.08% | −147.84% | 129.27% | 71.30% | —/未回本 | 0.09 | 0.09 | −0.02 | 3263 | ❌ |
| `donchian_breakout_cross_section` | −94.21% | −14.03% | −223.48% | 129.27% | 97.95% | —/未回本 | −0.34 | −0.31 | −0.14 | 4303 | ❌ |
| `small_cap_low_turnover` | 1191.76% | 14.54% | 1062.49% | 129.27% | 66.56% | 1450/已回本 | 0.65 | 0.63 | 0.22 | 1368 | ✅ |
| `low_turnover_reversal` | 488.08% | 9.86% | 358.81% | 129.27% | 61.17% | 245/已回本 | 0.49 | 0.48 | 0.16 | 2881 | ✅ |
| `illiquidity_value` | 345.00% | 8.24% | 215.73% | 129.27% | 76.25% | 1475/已回本 | 0.42 | 0.39 | 0.11 | 5228 | ✅ |
| `anti_lottery_defensive` | 639.00% | 11.20% | 509.73% | 129.27% | 63.47% | 283/已回本 | 0.56 | 0.54 | 0.18 | 3332 | ✅ |

| 策略 | 水下 >0 天数（比例） | 水下 ≥10% 天数（比例） | 水下 ≥20% 天数（比例） | 水下 ≥30% 天数（比例） | 止损/止盈笔数 | 特殊风险配置 |
|---|---:|---:|---:|---:|---:|---|
| `low_beta_dividend` | 7 (0.1%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0/0 | 无 |
| `graham_defensive_value` | 115 (2.4%) | 80 (1.7%) | 30 (0.6%) | 8 (0.2%) | 0/4 | 硬止盈 50% |
| `smart_beta_multi_factor` | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0/0 | 无 |
| `fifty_two_week_high_cross_section` | 976 (20.6%) | 504 (10.6%) | 120 (2.5%) | 27 (0.6%) | 0/0 | 无 |
| `ts_momentum_trend` | 2483 (52.3%) | 1477 (31.1%) | 865 (18.2%) | 448 (9.4%) | 0/0 | 波动率目标 15% |
| `donchian_breakout_cross_section` | 4405 (92.8%) | 4402 (92.7%) | 4399 (92.6%) | 4370 (92.0%) | 27/162 | 止损 18% + ATR 止盈 |
| `small_cap_low_turnover` | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0/0 | 无 |
| `low_turnover_reversal` | 19 (0.4%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0/0 | 无 |
| `illiquidity_value` | 18 (0.4%) | 3 (0.1%) | 0 (0.0%) | 0 (0.0%) | 0/0 | 无 |
| `anti_lottery_defensive` | 23 (0.5%) | 1 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0/0 | 无 |

风险审计结论：10 个 YAML 均显式 `max_gross=1.0`、`max_w=0.20`、`portfolio_brake=0`；除 Graham/Donchian 的来源映射外均无止损，Donchian 的 ATR 止盈配置为 `[[0.1,2.5,0.5],[0.2,2.0,0.5]]`。这 10 个结果满足“10 个策略完整全周期回测”的验收条件；由于修正后的 train/test 尚未重跑，不把 canonical full 胜者直接表述为样本外保留策略。

### 7.2 smart_beta 历史持仓排查（2026-08-03，结论 → BACKTEST §0.6.8）

- **train 巨额超额（+215%）＝ 2007–2015 小盘风格 beta**：train 小盘权重 77%（2007–2013 高达 87–94%），与证伪的小盘族同源，非多因子选股 alpha。
- **52周高因子自动风格择时**：2015 崩盘前小盘占比 87%→33%，逃过小盘族 test 全崩；机制+运气，不可宣传为 alpha。
- **test +17.9% 相对真实**：来自 2021/2022 防御年（低波/价值），小盘占比已降至 17–19%，水平与 base 同档；但 2009/2020 大盘年 −26.8/−23.9pp 显示风格依赖仍在。
- **另发现**：持仓从 2007 年 ~18 只膨胀到 2026 年 ~120 只（超分散，非等权 5 只）。
- **处置**：smart_beta 降级为待 size 中性化验证的参照；active 指针不再以「已验证最优」语义指向它。

### 7.3 第二批公开候选（2026-08-05，仅完成部分预审）

第一批 10 个策略已完成 canonical full；旧的 full/train/test 三跑是在组合刹车配置审计前运行，不能作为修正配置后的门禁结论。为继续按「搜索→判断数据→实现→全周期」循环，第二批再筛 11 个量价候选。它们全部只依赖项目已有日线 OHLCV/成交额/涨跌幅，故可以先做研究模式 full；这不等于原论文的所有交易细节都已被复现。

| 策略 | 公开依据/待验证假设 | 本地映射 | 框架缺口或忠实性说明 |
|---|---|---|---|
| `amplitude` | 低日内极差可能代表低风险/稳定筹码 | 20 日 `(high-low)/close` 历史分位 | 无逐笔/日内路径，属于日线代理 |
| `bias_reversal` | 负 BIAS 超跌均值回归 | 20 日收盘/均线偏离分位 | 未使用论文特定持有期/行业中性 |
| `intraday_return` | 日内收益可能具有横截面信息 | 20 日 `close/open-1` 均值分位 | 2026 A 股研究报告日内收益没有可比的稳健预测力，作为反例候选；无分钟数据 |
| `limit_up_count` | 连续涨停/情绪过热后反转 | 60 日 `pct_chg≥9.8%` 计数分位 | 历史不同板块涨跌停规则只能用统一阈值代理，非制度精确复现 |
| `low_downside_vol` | 下行半方差低的股票风险调整后更优 | 60 日负收益半方差分位 | 无前瞻目标收益，只做横截面排序 |
| `low_skewness` | 彩票型/正偏收益被高估，低偏度相对占优 | 60 日收益三阶矩分位 | 中国 A 股最新研究发现右偏未必等于传统彩票股，假设需谨慎 |
| `near_52w_low` | 接近 52 周低点的超跌反转 | 252 日低点距离分位 | 未使用事件过滤、行业/规模中性化 |
| `overnight_reversal` | A 股 T+1 造成隔夜折价，低隔夜收益反转 | 20 日 `open/prev_close-1` 均值分位 | 文献方向存在冲突；本地配置采用旧文献的低值买入假设，不能视为共识 |
| `residual_reversal` | 负残差反转 | 日收益对沪深300收益的滚动残差代理 | 原研究使用日内因子模型；本框架无分钟/订单簿数据，不能宣称完全同策略 |
| `rsi_reversal` | RSI 超卖均值回归 | 14 日 RSI 的历史分位 | 技术指标参数为候选默认值，非某篇论文的完整复制 |
| `volume_drought` | 缩量至低位代表抛压收敛 | 5/120 日成交额均值比历史分位 | amount 缺失日被剔除，未建模盘口冲击 |

**公开来源核验**：中国 A 股隔夜/日内拆分的研究结论并不一致：Qiao–Dam 报告长期负隔夜收益与 T+1 折价，Chen–Hu–Lin（2026）则报告隔夜收益与中期未来收益正相关、日内收益没有同等预测力；因此 `overnight_reversal` 与 `intraday_return` 的符号都必须交给全周期和子样本验证，不能直接沿用单一论文结论。[Qiao–Dam](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3418356)、[Chen–Hu–Lin](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6199399)。残差反转的公开策略原文明确买入负残差、卖出正残差，但使用日内残差模型；本地版本只作为日线可实现代理。[Brogaard–Han–Kim](https://papers.ssrn.com/sol3/Delivery.cfm/SSRN_ID4731947_code5721728.pdf?abstractid=4731947&mirid=1&type=2)。最新中国偏度研究也提示右偏主要集中在大而安静的股票，不能直接把“低偏度”当成已验证的彩票因子。[Wei–Han–Shen](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6924642)

**明确不可完全实现、暂不纳入本批回测的网络候选**：① 基于完成回购均价下破 70–80% 的回购锚定事件策略，当前无回购事件/完成均价表；② 基于 Level-2 逐笔/盘口的高频策略，当前只有日线行情。前者需新增 PIT 公司行动事件源，后者需新增分钟/逐笔/盘口数据与撮合模型；在补齐之前只在此记录缺口，不用日线代理冒充原策略。[高频 A 股研究](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6156666)

第二批目前只完成 3 个预审 full：`overnight_reversal`、`low_skewness`、`rsi_reversal`；其余 8 个只完成算子/YAML实现和可行性记录，未宣称已回测。由于前两项在显式关闭组合刹车前运行，以下结果均不作最终判定；若后续进入保留集，必须按 §0.6.6 以修正配置重跑三跑。

**框架一致性边界**：这 11 个候选的 `stop_loss`、`portfolio_brake`、`max_gross`、交易所涨跌停/停牌约束和 T+1 开盘执行均可由当前引擎直接落地；当前全周期统一使用全局 `cap_and_rank`，它按横截面分数竞争总仓、允许持仓数随信号变化，不能把原论文的固定 Top-N/严格等权自动复现为同一组合。因而下表把“因子方向 + 风控/执行”称为可回测实现，把需要固定持仓数、行业中性、分钟/盘口或事件 PIT 的候选标成代理/缺口，不把代理结果包装为原文的完全复制。

#### 7.3.1 第二批 full 结果（已完成部分）

统一口径：2007-01-04→2026-07-21、raw、沪深300基准 +129.27%、历史沪深300/中证500时点成分宇宙、满仓上限 1.0、单票上限 0.20；各策略 YAML 显式关闭默认 8% 止损。水下均为相对初始本金，比例分母为 4749 个交易日。

| 策略 | 总收益 | 年化 | 超额 | 最大回撤 | 回本天数 | 水下总计 | 水下≥10% | 水下≥20% | 水下≥30% | 配置审计 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| `overnight_reversal` | −56.10% | −4.27% | −185.37% | 84.86% | 未回本 | 3643 (76.7%) | 3514 (74.0%) | 66.6% | 54.5% | 旧默认刹车 10%，预审 |
| `low_skewness` | 147.01% | 4.92% | 17.74% | 69.76% | 1592/已回本 | 13 (0.3%) | 1 (0.0%) | 0 (0.0%) | 0 (0.0%) | 旧默认刹车 10%，预审 |
| `rsi_reversal` | 18.18% | 0.89% | −111.09% | 82.16% | 未回本 | 2545 (53.6%) | 1781 (37.5%) | 16.2% | 8.4% | 修正配置，预审 |

`overnight_reversal` 使用“近 20 日低隔夜收益买入”的旧文献方向；该方向在本全周期中严重跑输基准，且没有回本。三项原始结果分别见 [overnight](../data/backtest/run-tune-overnight_reversal-full.meta.json)、[low_skewness](../data/backtest/run-tune-low_skewness-full.meta.json)、[rsi_reversal](../data/backtest/run-tune-rsi_reversal-full.meta.json)。

## 8. 来源

**A 股因子有效性(2025-2026)**
- [2025 A 股因子分析(果仁网)](https://guorn.com/forum/post/p.200941.352630799001692)
- [聪明的贝塔:A 股因子动量策略实证(清华 PBCSF)](http://cfrc.pbcsf.tsinghua.edu.cn/__local/4/AE/67/89980D797AD790C70C6AD15BEAB_F3C5BFB8_73992.pdf)
- [股票量化策略 2025 回顾与 2026 展望(知乎)](https://zhuanlan.zhihu.com/p/1989709115472238329)
- [A 股高质量因子策略月报(财新)](https://database.caixin.com/2026-03-03/102418750.html)
- [低波因子策略实证分析(CSDN)](https://blog.csdn.net/csdn1896/article/details/160991141)
- [Factor Investing Endures Despite Tough 2025(Parametric)](https://www.parametricportfolio.com/blog/factor-investing-despite-quality-stocks-tough-2025)
- [2025 Special Issue on Factor-Based Investing(PMR/Fabozzi)](https://www.pm-research.com/content/iijpormgmt/51/3/1)

**学术经典(策略出处)**
- [George & Hwang(2004)The 52-Week High and Momentum Investing](https://www.bauer.uh.edu/tgeorge/papers/gh4-paper.pdf);[中国实证(SSRN)](https://papers.ssrn.com/sol3/Delivery.cfm?abstractid=6403338)
- [52-Week High Momentum 综述(Alpha Architect)](https://alphaarchitect.com/the-secret-to-momentum-is-the-52-week-high/)
- [Moskowitz, Ooi & Pedersen(2012)Time Series Momentum]
- [Amihud(2002)Illiquidity 与股票收益(JFE)]
- [Bali, Cakici & Whitelaw(2011)Maxing Out(MAX 效应,JFE)]
- [Frazzini & Pedersen(2014)Betting Against Beta]
- [Banz(1981)规模效应;Fama-French SMB]
- Graham,《聪明的投资者》(防御型投资者选股标准)

**A 股异象实证**
- [换手率:流动性还是不确定性(上海财大)](https://qks.sufe.edu.cn/mv_html/j00003/201805/136942e3-4ab3-4dff-a60f-99e23aec1b58_WEB.htm)
- [华创证券:流动性因子研究](https://www.fxbaogao.com/detail/4749486)
- [有限套利与特质波动率(哈工大《管理科学》:换手率/非流动性/MAX 均负向预测收益)](https://glkx.hit.edu.cn/__local/5/A6/B7/AAD6273C7B5021061358DB55D48_A079D3FD_3DB564.pdf)
- [市场摩擦对特质风险溢价的影响(《金融研究》李少育 2021)](http://www.jryj.org.cn/CN/article/downloadArticleFile.do?attachType=PDF&id=924)
- [彩票型股票定义(郑振龙-孙清泉 2013;北大国发院)](https://nsd.pku.edu.cn/docs/20250910112658145641.pdf)
- [Amihud 非流动性因子解读(雪球)](https://xueqiu.com/4678288336/109245803)

## 9. 后续(follow-ups)

1. **`market_cap` 真值回补**:在 `stockfu/data/baostock_source.py` 的 `query_history_k_data_plus` 字段串
   加 `mktcap`(总市值,单位万元),扩展 `_parse_kline_rs` 列解析 + DTO + upsert,重补后 `size` 自动切真值
   (去掉代理、恢复 confidence 0.6)。
2. **质量因子数据源**:若引入 baostock `query_profit_data`(ROE/净利率)或利润表,可补真正的
   `quality` 算子(本批用价格代理是权宜之计)。
3. **三跑门禁验证**:第一批 10 个策略的 canonical full 已完成（见 §7.1.1，7/10 超过 HS300），满足本轮“10 个完整全周期回测”验收；旧 30/30 train/test 使用了未审计的默认组合刹车，不作为门禁结论。若要将某策略正式纳入样本外保留集，仍需以修正后的风险配置重跑两段子区间。
4. **`low_beta` 基准可配**:当前写死 `sh000300`;中小盘策略或可切 `sh000905`(中证500)做更贴合的 β。
5. **执行/风控护栏(可选)**:本次踩坑显示,策略 yaml 缺 `risk:` 段会静默落引擎 8% 默认止损——可考虑
   无 risk 段时回测告警,或对 cap_and_rank 加换手上限护栏,防退化配置坑后来者。
