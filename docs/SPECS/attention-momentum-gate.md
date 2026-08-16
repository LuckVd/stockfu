# 注意力动量进攻三段门禁结果（2026-08-16）——不通过

> 判定：**fail（三段方向一致地巨额亏损）**。形态保留为研究资产，不进正式保留集。

## 口径

- alpha `attention_momentum_v2`：turnover_20d_high 0.5 + momentum_60d 0.3 +
  low_volatility_20d 0.2（软约束），`pf_daily_top15_slow21_v2`（top15、min_hold=21、
  minimum_score=60）+ `no_overlay_v1`
- 三段固定区间（`docs/BACKTEST.md` §0.6.6）、hs300 历史成分 PIT 宇宙、快照
  `stockfu-30017a165740`（sha256:30017a165740…，数据到 2026-08-14）、观察窗 271、
  canonical=true（git clean 提交 0e39295/14b1bab 绑定）、T+1 开盘执行、qfq 估值、
  涨跌停/停牌/整手/费用/滑点按引擎主线
- 产物：`data/backtest/v2-segments/run-20260816-212244-382097/`（suite.json complete、
  canonical=true；日志 `data/backtest/attention-momentum-segments*.log`）

## 结果

| 段 | 总收益 | 年化 | 最大回撤 | Sharpe |
|---|---:|---:|---:|---:|
| full (2013-2026) | **-55.54%** | -6.51% | **93.03%** | -0.05 |
| 2013-2019 | **-62.88%** | -16.01% | 85.27% | -0.44 |
| 2020-2026 | **-47.72%** | -11.54% | 71.89% | -0.24 |

年度收益（full 段正式期）：2015 -28.8% / 2016 -38.3% / 2017 -22.3% / 2018 -40.7% /
2019 +19.1% / 2020 +22.6% / 2021 -12.2% / 2022 -39.3% / 2023 -14.1% / 2024 +4.8% /
2025 +82.3% / 2026 +11.7%。

诊断：raw missing 极低（turnover_20d formal 0.2%、momentum_60d 0.005%）、coverage
正常、成交 9042 笔（月频 top15 合理）、无风控触发（no_overlay）。

## 判定与归因

1. **门禁判定：fail**。三段方向一致且全部巨亏，回撤 93% 属"回撤失控"
   （§0.6.6：任一子段超额反向/Sharpe 明显衰减/回撤失控 → 不进入正式保留集）。
2. **形态本质**：高换手×动量 = 牛市进攻 beta。牛/反弹年（2019/2020/2025/2026）
   赚钱，熊/震荡年（2015-2018、2021-2023）追高接盘被反复闷杀；13 年仅 5 年正收益。
3. **IC 快验与回测的分歧**（重要教训）：
   - IC 快验的"高换手×高动量 fwd20 中性化 +1.86%"是**每日截面重平衡的多空价差**，
     剥离了市场共同成分（beta）；
   - 实际策略是**月频 top15 集中持仓、long-only 全额单边**，吃满 beta 且无 overlay
     保护——中性化价差的正期望无法传导到单边持仓；
   - 高换手票冲击/滑点成本高于均值（9042 笔成交累计）；
   - IC 快验 2021-24 熊市段多空仅 +1.06（时变性），回测中该段亏损 12~39%/年。
4. **与既有结论一致**：10 策略研究（动量崩溃/接飞刀）、growth-offense 收敛结论
   （"任何风控压仓都是收益换回撤的零和交易"）——无保护的进攻形态在 A 股
   long-only 下系统性负期望，本形态是同一规律的又一例证。

## 保留资产

- `turnover_20d` raw（换手均值，独立因子维度，IC 判别成立：fwd5 强负/fwd20 强正）
- `turnover_20d_high_v2` profile、`attention_momentum_v2` alpha 配置（可复现对照）
- `scripts/turnover_attention_ic.py` 判别脚本（可复跑）
- 未来用途：换手率作为**防守/排雷维度**（fwd5 拥挤惩罚：高换手短期跑输）或
  事件策略的确认器，比进攻主轴更符合其真实信号方向；若做进攻须叠加强风控
  overlay 且先跑「vs no_overlay 风险调整净胜」基线（growth-offense 同范式）。

## 复现

```bash
env TMPDIR=/tmp python3 main.py --backtest-v2-segments attention_momentum_v2 \
  --codes hs300 --snapshot data/snapshots/stockfu-30017a165740.db \
  --observation-count 271 --canonical
```
