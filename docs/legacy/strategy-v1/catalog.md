# V1 策略归档目录（strategy-v1）

> 依据 docs/SPECS/factor-strategy-score-v2.md §17。V1 的 52 份 YAML 不作为 V2 活跃配置复用；
> 本目录把配置与结论冻结为可校验产物。删除门禁（§17.3）通过前，V1 代码与缓存不得删除。

- 生成时间：2026-08-07T14:37:12+08:00
- git commit：`678d548e08b818c8f295d0a9d6d2abb5c5ec030f`（dirty=True）
- 源文件：52 个基础 YAML（`stockfu/ai/strategies/` 全部收录），seed 选择 29 个，展开后 57 条配置
- 保留运行 id：31 个（seed._RETAINED_STRATEGY_IDS）

## 文件说明

| 文件 | 说明 |
|---|---|
| catalog.yaml | 全部展开策略的完整有效配置（含隐式默认展开，机器可读主表） |
| catalog.md | 本文件：中文目的、缩写展开、已知结论、迁移方向 |
| strategy-source/ | 原 52 个 YAML 只读副本（保留注释） |
| runtime-bindings.yaml | rebalancer/宇宙/调度入口/费用口径等运行绑定 |
| result-index.csv | 历史回测 artifact 索引（路径/指标/checksum） |
| migration-map.yaml | old id -> archive_only / V2 组合映射 |
| checksums.sha256 | 全部产物 + 原 YAML 的 SHA-256 |

## §18.1 公共配置缩写展开

- **W12**：weighted_sum thresholds strong_buy=12/buy=4/hold=-4/sell=-12
- **W8**：weighted_sum thresholds 8/3/-3/-8
- **W10**：weighted_sum thresholds 10/4/-4/-10
- **D1**：buy_cool_down=1/sell_cooldown=1/max_target_step=1.0/risk_confirm=1/min_trade_weight=0.01/conf_gate=0.0
- **Drot**：buy=5d/sell=3d/max_target_step=0.3/risk_confirm=1/min_trade_weight=0.01/conf_gate=0.3
- **Dmacd**：buy=3d/sell=0d/max_target_step=1.0/risk_confirm=1/min_trade_weight=0.01/conf_gate=0
- **Dhold**：buy=30d/sell=30d/max_target_step=1.0/risk_confirm=1/min_trade_weight=0.01/conf_gate=0
- **P20**：position continuous max_w=0.20 dead=3 score_full=8
- **P05**：position continuous max_w=0.05 dead=3 score_full=8
- **Prot12**：position continuous max_w=0.12 dead=3 score_full=8
- **Prot10**：position continuous max_w=0.10 dead=3 score_full=8(隐式)
- **R0**：risk stop_loss=0/portfolio_brake=0/max_gross=1.0
- **Rimplicit**：risk 段未写，运行时采用引擎默认（stop_loss=0.08/brake=0.10/scale=0.50/max_gross=0.90）
- **H1**：dividend_yield[1.0; high_yield=5.0,price_basis=raw,yield_cap=20.0] + low_volatility[0.8;20,3y] + value[0.6;5y]，W12
- **H2**：H1 三因子权重均 1.0；sell_weights dividend_yield=2/low_volatility=1/value=2

## 迁移总览（migration-map.yaml 摘要）

| 处置 | 策略数 | 策略 |
|---|---|---|
| keep | 8 | momentum_breakout, cn_momentum_cross_section, donchian_breakout_cross_section, etf_momentum_cross_section, etf_momentum_rotation, momentum_breakout_cross_section, reversal_cross_section, reversal_strategy |
| v2_candidate | 20 | dividend_cross_section, dividend_cross_section_take_profit, dividend_cross_section_partial_take_profit, dividend_cross_section_atr_take_profit, dividend_cross_section_atr_lagged_take_profit, dividend_cross_section_partial_brake_take_profit, dividend_cross_section_partial_gentle_brake_take_profit, dividend_cross_section_partial_selective_brake_take_profit, dividend_cross_section_partial_exposure_brake_take_profit, dividend_cross_section_partial_drawdown_add_gated_take_profit, dividend_cross_section_partial_exposure_add_gated_take_profit, dividend_cross_section_partial_exposure_brake_hold_take_profit, dividend_cross_section_partial_exposure_add_gated_hold_take_profit, dividend_cross_section_partial_exposure_brake_regime_trend_take_profit, dividend_cross_section_partial_exposure_brake_regime_vol_take_profit, dividend_cross_section_partial_exposure_brake_regime_trendvol_take_profit, low_turnover_reversal, ts_momentum_trend, cross_section_factor, dividend_low_vol |
| rebuild | 9 | dual_bollinger, small_cap_low_turnover, illiquidity_value, anti_lottery_defensive, low_beta_dividend, bollinger_reversion, bollinger_reversion_cross_section, macd_cross, residual_reversal |
| research | 10 | amplitude, bias_reversal, intraday_return, limit_up_count, low_downside_vol, low_skewness, near_52w_low, overnight_reversal, rsi_reversal, volume_drought |
| archive_only | 5 | fifty_two_week_high_cross_section, graham_defensive_value, smart_beta_multi_factor, cn_momentum_rotation, pure_factor |

## 已知结论与复现

- 通用策略已证伪（archive_only）：cn_momentum_rotation、fifty_two_week_high_cross_section、pure_factor、smart_beta_multi_factor、graham_defensive_value
- 历史回测结论见 docs/BACKTEST.md（§0.6.x 归档章节）与 result-index.csv
- 复现：`git checkout {_git_head()} && python main.py --backtest <strategy_id> --start --end --cash`（数据快照见 data/backtest/ 产物）
