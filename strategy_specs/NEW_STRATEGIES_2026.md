# 2026-08 网络调研新增策略族 · 算子与策略文档

> 配套调研与实现说明见 [`docs/STRATEGY_RESEARCH_2026.md`](../docs/STRATEGY_RESEARCH_2026.md)(学术出处、
> 选型理由、数据缺口、回测口径与门禁)。本文件聚焦**算子接口与策略配置**。
>
> 全部 10 个新算子位于 `stockfu/ai/operators/factors/`,通过 `@register` 自注册;10 个策略 yaml 位于
> `stockfu/ai/strategies/`,已登记进 `seed.py` 的 `_STRATEGIES` + `_RETAINED_STRATEGY_IDS`。

## 0. 通用约定

- **横截面范式**:10 个新策略均沿用 `cap_and_rank` 横截面模板——`operators / aggregate(weighted_sum) /
  position(max_w 0.20, score_full 8) / debounce(冷却 1 日)`。universe 与 rebalancer 由运行配置决定
  (默认 `historical_indices` 沪深300+中证500 并集 + `cap_and_rank`)。
- **score 刻度**:全部对齐 ±20 满强度(与 momentum/reversal/low_volatility/value 一致),`weighted_sum`
  按 YAML `weight` 加权,`rebalancer` 横截面排序选头部。`score_full=8` 表示个股 total_score 达 8 即满 `max_w`。
- **防未来函数**:所有取数走 `quote_series(code, field, days, as_of=ctx.as_of)`,严格 `<= as_of`。
- **回测口径**:收益/净值走 **qfq**(默认 CLI);三跑门禁统一 `--valuation-basis raw`(与 §0.6.x 全族对齐,
  对齐分红)。股息率分母用 **raw**(防 qfq 前视)。详见 `docs/BACKTEST.md §0`。

## 1. 新算子清单(10 个,可被任意策略复用)

| operator_id | 文件 | 数据字段 | 满强度刻度 | 一行原理 |
|---|---|---|---|---|
| `fifty_two_week_high` | factors/fifty_two_week_high.py | close(qfq) | ratio 0.7→0,1.0→+20 | 距 52 周高点近度(George-Hwang) |
| `ts_momentum` | factors/ts_momentum.py | close(qfq) | z=ret/(vol·√w),±2→±20 | 风险调整时序动量(TSMOM) |
| `momentum_acceleration` | factors/momentum_acceleration.py | close(qfq) | accel 1%≈1.5 分,钳±20 | 二阶动量(近段−远段收益) |
| `donchian_breakout` | factors/donchian_breakout.py | close(qfq) | pos 0.5→0,1.0→+20 | 唐奇安通道位置(海龟突破) |
| `lottery_max` | factors/lottery_max.py | close(qfq) | MAX 5%→0,8%→−20(仅罚多头) | MAX 彩票股(反向,Bali-Cakici-Whitelaw) |
| `low_beta` | factors/low_beta.py | close + sh000300(按日对齐) | β 0.5→+20,1.0→0,1.5→−20 | 相对沪深300 低贝塔(防御) |
| `size` | factors/size.py | market_cap(空则 amount×100/turnover 代理) | log9.5→+20,11.5→0,12.5→−10 | 小市值(规模异象) |
| `low_turnover` | factors/low_turnover.py | turnover | 0.5%→+20,3%→0,8%→−15 | 低换手率(流动性异象) |
| `illiquidity` | factors/illiquidity.py | amount + close | log(ILLIQ·1e9)+1)×10,钳[−10,20] | Amihud 非流动性溢价 |
| `graham_value` | factors/graham_value.py | PE/PB(分位)+ 股息 | (PE/PB 分位子分±8)+ 股息加分≤+4 | 格雷厄姆防御价值(复合) |

### 1.1 关键实现细节

- **`low_beta`**:基准 `sh000300`(沪深300)序列按 `(as_of, length)` 进程内缓存(回测内每日仅 1 次 DB,
  全 universe 共享,避免 N×M 的 N+1);**按日期交集对齐** stock/bench(`quote_series_dates`),杜绝长度
  不等时末段截断错配导致 β 失真。
- **`size`**:当前 DB 的 `market_cap` **全库为空**(baostock 回补字段串未含 `mktcap`,见调研文档 §5 数据
  缺口)。算子优先读 `market_cap`,空则用派生代理 `amount×100/turnover`(=总股本×价 ≈ 市值,取 20 日均)。
  待 baostock 加 `mktcap` 字段重补后自动切真值。代理期 `confidence` 降到 0.55。
- **`ts_momentum` / `momentum_acceleration` / `low_beta`**:窗口缓冲按交易日↔日历日比放大
  (`int(window*1.5)+30`),否则 `quote_series` 的日历日缓冲在大窗口(120 日)下拿不满所需交易日。
- **`lottery_max`**:效应集中在空头端(MAX 高→跑输),故**仅惩罚高 MAX**(负分),低 MAX 中性
  (不强推),避免把低 MAX 大盘股误推入多头。
- **`graham_value`**:复用 `valuation_snapshot`(PE/PB 分位,回测 provider 零 DB)+ `dividend_yield_ttm`
  (股息 provider),无额外数据依赖。

## 2. 策略清单(10 个)

| 策略 id | yaml | 算子(weight) | 主题 |
|---|---|---|---|
| `fifty_two_week_high_cross_section` | fifty_two_week_high_cross_section.yaml | 52w_high 1.0 + momentum 0.6 + trend_strength 0.4 | 近高点动量(George-Hwang) |
| `small_cap_low_turnover` | small_cap_low_turnover.yaml | size 1.0 + low_turnover 0.8 + reversal 0.5 | 小盘 + 流动性 |
| `low_turnover_reversal` | low_turnover_reversal.yaml | low_turnover 1.0 + reversal 0.8 + value 0.6 | 低换手 + 反转 + 低估 |
| `illiquidity_value` | illiquidity_value.yaml | illiquidity 1.0 + value 0.8 + reversal 0.5 | 非流动溢价 + 价值 |
| `anti_lottery_defensive` | anti_lottery_defensive.yaml | lottery_max 1.0 + low_volatility 0.8 + value 0.6 | 反彩票 + 防御 |
| `low_beta_dividend` | low_beta_dividend.yaml | low_beta 1.0 + dividend_yield 0.8 + value 0.6 | 低贝塔 + 红利(防御收入) |
| `ts_momentum_trend` | ts_momentum_trend.yaml | ts_momentum 1.0 + momentum_acceleration 0.6 + trend_strength 0.4 | 风险调整趋势跟踪 |
| `graham_defensive_value` | graham_defensive_value.yaml | graham_value 1.0 + dividend_yield 0.6 + low_volatility 0.5 | 格雷厄姆深价值 |
| `donchian_breakout_cross_section` | donchian_breakout_cross_section.yaml | donchian 1.0 + ts_momentum 0.6 + trend_strength 0.4 | 通道突破趋势 |
| `smart_beta_multi_factor` | smart_beta_multi_factor.yaml | 52w_high 0.6 + low_vol 0.8 + value 0.6 + low_turnover 0.6 + graham 0.4 (score_full 10) | 多因子分散(Smart Beta) |

### 2.1 每个策略的「为什么这么配」

- **fifty_two_week_high_cross_section** — George-Hwang 原意:近高点本身已含动量信息,叠 `momentum`
  做方向确认、`trend_strength`(多头排列)做结构确认,过滤「假突破」。
- **small_cap_low_turnover** — 小盘超额最怕流动性陷阱(2024 初微盘崩跌),`low_turnover` 反向滤掉
  高换手投机票,`reversal` 滤掉短期暴涨见顶票。
- **low_turnover_reversal** — 低换手 + 短期反转 + 低估三者 A 股实证同向(被错杀的稳健票)。
- **illiquidity_value** — 非流动溢价与价值同向(被冷落的低估票常兼流动性差);**勿与 low_turnover
  叠加过重**(二者高度相关,华创/《金融研究》实证控制流动性后换手率效应减弱)。
- **anti_lottery_defensive** — `lottery_max` 负向剔除彩票股,`low_volatility` + `value` 做防御多头。
- **low_beta_dividend** — 低 β + 高息 = 防御收入;**牛市/成长行情会跑输基准**,适合震荡/熊市。
- **ts_momentum_trend** — TSMOM 内核(波动归一)+ 加速确认 + 多头排列,区别于 `cn_momentum`
  (原始动量)更稳健(惩罚高波动)。
- **graham_defensive_value** — 深低估(PE/PB 分位低)+ 派息 + 低波,安全边际导向。
- **donchian_breakout_cross_section** — 通道突破 + 趋势确认;区别于 `momentum_breakout`(月布林 +
  top_n 锁仓),本策略是日线通道 + 横截面纪律换仓。
- **smart_beta_multi_factor** — 5 因子分散暴露,目标稳健 Sharpe 而非单期最高收益(各因子互相稀释)。

## 2.2 风控与仓位配置(逐来源核对原文)

**共同点**:9 个纯学术因子策略**原文无止损**,故 `risk.stop_loss: 0` 显式关闭引擎默认 8% 止损;
`risk.max_gross: 1.0` 满仓(无现金垫,覆盖全局 app_config 0.95);`position.max_w: 0.20` 等权 ~5 只;
**无组合刹车**。仅保留原文真实存在的机制:

| 策略 | 风控(对照原文) |
|---|---|
| 9 个纯因子(52周高/小盘/低换手/非流动/反彩票/低贝塔/smart_beta) | 无止损、满仓等权、无刹车(原文无风控,靠持有+再平衡) |
| `ts_momentum_trend` | 无止损 + `market_regime_target_vol: 0.15`(MOP 波动率目标缩放) |
| `graham_defensive_value` | 无止损 + `take_profit.hard_profit: 0.50`(格雷厄姆「涨 50% 走」) |
| `donchian_breakout_cross_section` | `stop_loss: 0.18`(海龟 2N ≈ 2×ATR20)+ ATR(20) 追踪止盈(跌破通道离场) |

> 修正过程:初版全部落引擎默认 8% 止损 → 三跑全负超额(因子 style 衰减 + 8% 洗盘 + 高换手);
> 曾改为 0.30 宽止损(非原文) → 最终按原文逐来源核对,改为上述配置。**结论口径**:风控/仓位必须是
> 各来源原意的映射,不能套全局默认。

## 3. 数据依赖速查

| 算子所需字段 | 是否已回补 | 备注 |
|---|---|---|
| close_qfq / open / high / low | ✅ 全市场 | baostock 三复权 backfill |
| pe / pb | ✅ 全市场(universe 内) | baostock peTTM/pbMRQ |
| turnover / amount | ✅ 全市场 | baostock turn / 成交额 |
| dividend_event(股息) | ✅ | `--backfill-dividend` |
| sh000300(指数) | ✅ | low_beta 基准 |
| **market_cap** | ❌ **全库空** | size 用 amount×100/turnover 代理;真值待 baostock 加 `mktcap` 重补 |

## 4. 运行方式

```bash
# 单策略回测(小宇宙快速验证)
BAOSTOCK_PROXY_MODE=direct python3 main.py --backtest fifty_two_week_high_cross_section \
    --start 2024-01-01 --end 2026-07-24 --codes 600519,000858,300750,000012,000028

# 全周期更新(默认 universe = historical_indices 沪深300+中证500 并集)
python3 main.py --update-backtests --strategies small_cap_low_turnover,anti_lottery_defensive

# 列出策略确认已入库
python3 main.py --list-strategies
```

> ⚠️ **认定结论前必须过三跑门禁**(全样本 2007–2026 + 两段不同行情的 ≥5 年子区间),方向一致才
> 认定,否则按 `docs/BACKTEST.md §0.6.4` 判过拟合。历史回测产物只作探索记录。
