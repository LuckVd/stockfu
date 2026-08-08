# V2 算子迁移边界（2026-08-06，修复后口径）

本文件记录 V2 vertical slice 的明确边界。本阶段不做全量算子迁移；未列为 active 的算子不得被 V2 runtime 自动扫描，也不得把 V1 `score` 当作 V2 raw 或 factor score。

## 范围决策（2026-08-07）

V1 因子不要求全量迁移到 V2。V2 采用选择性迁移：只有明确的研究或业务需求、且完成
raw 点时边界、单位/缺失规则、profile 映射、无未来测试和回归验收的因子，才进入 V2
active registry。未迁移因子继续作为 V1 归档或兼容实现保留，不构成 V2 未完成的阻塞项，
也不得为了追求覆盖率而把旧 `score` 静默接入 V2 评分链路。

从本决定起，新的 V1 回测统一拒绝：`--backtest`、`--update-backtests`、V1
`backtest.scheduler.run()` 和 `backtest.engine.run_backtest()` 均 fail-closed，后续回测
必须使用 V2 alpha/config/portfolio/risk 链路。V1 策略、算子和历史产物暂保留用于兼容读取、
信号/评估等尚未迁移的业务路径，不再作为新回测入口。

## 当前已绑定 raw metric

| V2 raw metric | profile | 状态 | 说明 |
|---|---|---|---|
| `low_beta` | `low_beta_v1` | active | 120 日、qfq、指定基准；参数进入 raw fingerprint |
| `dividend_yield_ttm` | `dividend_yield_ttm_v2` | active | 365 日 ex-date TTM、raw close；窗口内无现金分红按有效 0% 参与评分，不启用当前行业历史分量 |
| `low_volatility_20d` | `low_volatility_20d_v2` | active | 20 日 qfq 年化波动率；不启用当前行业历史分量 |
| `value` | `value_v1` | compatibility | 仍是 V1 PE 历史分位，仅用于 `low_beta_dividend_v2` 对照；不满足 earnings_yield/book_to_price 拆分要求 |

`dividend_low_vol_v2` 是当前 canonical vertical slice；`dividend_low_vol_v1` 和旧的 v1 profile 仅作兼容复现。`low_beta_dividend_v3` 是点时安全的比较 alpha；`low_beta_dividend_v2` 保留为 V1 对照，不代表 V2 value 设计已经完成。

## 尚未迁移/不进入 active V2 registry

以下名称来自设计 §12，当前没有 V2 raw/profile 绑定：

`amplitude`、`bias_reversal`、`daily_bollinger`、`distance_from_low`、`donchian_breakout`、`downside_skewness`、`downside_volatility`、`fifty_two_week_high`、`illiquidity`、`intraday_return`、`limit_up_count`、`lottery_max`、`low_turnover`、`macd_cross`、`mean_reversion`、`momentum`、`momentum_acceleration`、`monthly_bollinger`、`overnight_return`、`residual_reversal`、`reversal`、`rsi_reversal`、`size`、`trend_linearity`、`trend_strength`、`ts_momentum`、`volume_drought`、`weekly_bollinger`。

`graham_value` 不是待兼容 raw：它是 V1 复合评分，V2 计划删除，由透明 alpha 组合替代。旧 `value` 后续应迁移为 `earnings_yield` + `book_to_price` 后再停用。

## V1 因子档案（保留实现，不进入 V2 评分）

下面的表是旧实现的“档案索引”，不是迁移计划。`source` 是仍保留的 V1
实现文件；`formula / 默认参数` 记录旧算子真正计算的值和默认旋钮；`status`
说明它在 V2 中的处理方式。旧算子输出的 `score` 通常是 `±10/±15/±20`
或带信号的连续值，V2 不直接读取这个 score，而是重新把 raw 映射为 0–100。

| V1 operator | source | formula / default params | status in V2 |
|---|---|---|---|
| `dividend_yield` | `stockfu/ai/operators/factors/dividend_yield.py` | TTM 现金分红 / raw price；`high_yield=5`、`yield_cap=20` | archive source；V2 counterpart=`dividend_yield_ttm` active |
| `low_volatility` | `stockfu/ai/operators/factors/low_volatility.py` | N 日收益标准差的历史分位；`window=20`、`hist_years=3` | archive source；V2 counterpart=`low_volatility_20d` active |
| `low_beta` | `stockfu/ai/operators/factors/low_beta.py` | 相对 `sh000300` 的滚动 beta；`window=120` | archive source；V2 counterpart=`low_beta` active |
| `value` | `stockfu/ai/operators/factors/value.py` | PE 历史分位；`years=5` | compatibility；V2 `value_v1` 暂保留对照 |
| `graham_value` | `stockfu/ai/operators/factors/graham_value.py` | PE + PB + 股息的复合价值分；`years=5` | archive_only；V2 不复用复合旧分 |
| `amplitude` | `stockfu/ai/operators/factors/amplitude.py` | `(high-low)/close` 均值的历史分位；`window=20`、`hist_years=3` | archive_only |
| `bias_reversal` | `stockfu/ai/operators/factors/bias_reversal.py` | 价格相对 N 日均线的 BIAS 历史分位；`window=20`、`hist_years=3` | archive_only |
| `daily_bollinger` | `stockfu/ai/operators/factors/daily_bollinger.py` | 日线 Bollinger 位置及上下轨突破；`window=20`、`std_dev=2`、`buy_max=.45`、`sell_min=.55` | archive_only |
| `distance_from_low` | `stockfu/ai/operators/factors/distance_from_low.py` | 收盘价距 N 日低点的距离历史分位；`window=252`、`hist_years=3` | archive_only |
| `donchian_breakout` | `stockfu/ai/operators/factors/donchian_breakout.py` | 收盘价在 Donchian 通道中的位置；`window=20`、`buy_pos=.8`、`sell_pos=.2` | archive_only |
| `downside_skewness` | `stockfu/ai/operators/factors/downside_skewness.py` | 日收益偏度的历史分位；`window=60`、`hist_years=3` | archive_only；已从 V1 registry 下线，孤儿缓存已清理 |
| `downside_volatility` | `stockfu/ai/operators/factors/downside_volatility.py` | 负收益样本标准差/半方差的历史分位；`window=60`、`hist_years=3` | archive_only |
| `fifty_two_week_high` | `stockfu/ai/operators/factors/fifty_two_week_high.py` | 收盘价 / 约 52 周最高价；`lookback=250`、`lo=.70` | archive_only |
| `illiquidity` | `stockfu/ai/operators/factors/illiquidity.py` | Amihud `mean(abs(return)/amount)`；`window=20` | archive_only |
| `intraday_return` | `stockfu/ai/operators/factors/intraday_return.py` | `close/open-1` 的 N 日均值历史分位；`window=20`、`hist_years=3` | archive_only |
| `limit_up_count` | `stockfu/ai/operators/factors/limit_up_count.py` | N 日涨停次数历史分位；`window=60`、`threshold=9.8%`、`hist_years=3` | archive_only |
| `lottery_max` | `stockfu/ai/operators/factors/lottery_max.py` | N 日最大单日收益，超过阈值转负分；`window=20`、`warn_max=5%`、`flag_max=8%` | archive_only |
| `low_turnover` | `stockfu/ai/operators/factors/low_turnover.py` | N 日平均换手率，低换手加分；`window=20` | archive_only |
| `macd_cross` | `stockfu/ai/operators/factors/macd_cross.py` | 日线 + 周线 MACD 金叉/死叉组合；`fast=12`、`slow=26`、`signal=9` | archive_only |
| `mean_reversion` | `stockfu/ai/operators/factors/mean_reversion.py` | RSI 超买超卖反向分；`rsi_period=14`、`oversold=30`、`overbought=70` | archive_only |
| `momentum` | `stockfu/ai/operators/factors/momentum.py` | N 日收益率；`window=20` | archive_only |
| `momentum_acceleration` | `stockfu/ai/operators/factors/momentum_acceleration.py` | 近段收益 − 远段收益；`window=120`（近/远各半） | archive_only |
| `monthly_bollinger` | `stockfu/ai/operators/factors/monthly_bollinger.py` | 月线聚合后 Bollinger 位置/突破；`window=20`、`std_dev=2` | archive_only |
| `overnight_return` | `stockfu/ai/operators/factors/overnight_return.py` | `open/previous_close-1` 的 N 日均值历史分位；`window=20`、`hist_years=3` | archive_only |
| `residual_reversal` | `stockfu/ai/operators/factors/residual_reversal.py` | 剔除市场 beta 后的残差收益均值历史分位；`window=20`、`beta_window=60`、`hist_years=3` | archive_only |
| `reversal` | `stockfu/ai/operators/factors/reversal.py` | N 日收益率取负；`window=20` | archive_only |
| `rsi_reversal` | `stockfu/ai/operators/factors/rsi_reversal.py` | RSI 的历史分位反转；`window=14`、`hist_years=3` | archive_only |
| `size` | `stockfu/ai/operators/factors/size.py` | 总市值历史分位，小市值加分；`window=20` | archive_only；当前 market_cap 数据不足，不能偷偷迁移 |
| `trend_linearity` | `stockfu/ai/operators/factors/trend_linearity.py` | 价格-时间线性回归 `r² × direction`；`window=20` | archive_only |
| `trend_strength` | `stockfu/ai/operators/factors/trend_strength.py` | MA5/MA10/MA20 多空排列；内部 lookback=250 | archive_only |
| `ts_momentum` | `stockfu/ai/operators/factors/ts_momentum.py` | 收益 / 波动的风险调整时序动量；`window=120` | archive_only |
| `volume_drought` | `stockfu/ai/operators/factors/volume_drought.py` | 短均量 / 长均量的历史分位；`short=5`、`long=120`、`hist_years=3` | archive_only |
| `weekly_bollinger` | `stockfu/ai/operators/factors/weekly_bollinger.py` | 周线聚合后 Bollinger 位置/突破；`window=20`、`std_dev=2`、`buy_max=.3`、`sell_min=.7` | archive_only |

### 档案使用规则

旧代码、旧 YAML 和仍有复现价值的缓存继续保留；它们可以用于解释历史策略或
复现 V1，但不能被 V2 registry 自动发现。V2 真正迁移某个因子时，必须另建
`raw metric + profile + version`，明确 raw 单位、点时数据边界、缺失规则和
0–100 映射；不能把本表的旧 `score` 当成迁移结果。

## 后续迁移门禁

每个算子迁移前必须单独完成 raw 点时边界、单位/缺失、旧 raw 对照、profile 分布和无未来测试；迁移后才能加入 V2 配置。未完成前保留 V1 代码和缓存仅供历史复现，不得被 V2 评分链路复用。
