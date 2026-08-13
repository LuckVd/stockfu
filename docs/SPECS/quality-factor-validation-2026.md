# 质量因子实证验证（2026-08-13）· 成分股宇宙

> 验证对象：`quality_roe`（最新年报 ROE − pstdev 近 5 年年度 ROE）、`gross_margin`、
> `leverage`（均 PIT 按公告日）。宇宙：中证500+沪深300 当前成分并集 1,720 只
> （IC 研究）/ 历史成分并集（回测）。脚本 `scripts/quality_factor_ic.py`。
> 前向收益 21 交易日，月末采样。

## 1. 单因子 IC（横截面 Spearman，月频）

| 区间 | quality_roe | gross_margin | leverage | earnings_yield(对照) |
|---|---|---|---|---|
| 2016–2026 全期 | IC 0.94% t=0.78 | 0.24% t=0.22 | -0.63% t=-0.58 | 3.19% t=1.89 |
| 2016–2019 | **2.97% t=1.58** | 2.35% t=1.36 | -1.31% t=-0.76 | 4.52% t=2.04 |
| 2020–2026 | -0.46% t=-0.29 | -1.41% t=-1.07 | 0.15% t=0.11 | 2.77% t=1.17 |
| 2024–2026 | 1.93% t=0.65 | -0.43% t=-0.25 | -0.26% t=-0.16 | 0.71% t=0.19 |

- **质量三因子全期不显著**（|t| < 1）；仅 2016–2019 早期 quality_roe 弱有效（t=1.58）。
- 分位收益单调性差、多空价差近零或为负；2024–2026 所有因子 Q1（最弱分位）收益最高，
  反映该阶段高估值/题材风格占优。
- 结论：**单因子无稳定 alpha，不足以单独成策略**（quality_v1 回测证实：年化 5.31%、
  回撤 57.3%、Sharpe 0.34）。

## 2. 质量 × 价值 相关性（横截面 Spearman vs earnings_yield）

| 因子 | 全期 mean_rho | 2024–2026 |
|---|---|---|
| quality_roe | **+0.42**（0.20–0.56） | +0.51 |
| gross_margin | -0.05 | -0.03 |
| leverage | +0.24 | +0.20 |

- **quality_roe 与 E/P 正相关**（银行等"低 PE + 稳定高 ROE"集群主导），与调研
  （全 A 多空口径"价值与盈利负相关"）方向相反——宇宙/口径差异所致。
- 正相关意味着质量并非价值的对冲面，而是同向强化；组合增益来自与 momentum/lowvol
  的交互而非价值分散。

## 3. 组合回测对照（2016-01-01→2026-06-30，300+500 历史成分，日调仓 top15、
   min_hold 21、观察窗 271、no_overlay，研究模式 non-canonical）

| 策略 | 年化 | 总超额 | 最大回撤 | Sharpe |
|---|---|---|---|---|
| multi_factor_v2（V+M+LV，无质量） | 7.34% | 44.8% | 28.9% | 0.53 |
| **multi_factor_quality_v2（+quality_roe 20%）** | **9.19%** | **76.4%** | 32.0% | **0.61** |
| quality_v1（纯质量） | 5.31% | 14.8% | 57.3% | 0.34 |

- 加质量极：年化 +1.85pct、超额 +31.6pct、Sharpe +0.08，回撤 +3.1pct（可接受）。
- **结论：质量因子适合作为多因子组合的一极，不适合单因子策略**；单次窗口结果，
  正式保留需按三段门禁重跑（见 WORKSTATE 下一步）。

## 4. 与调研的差异解释

调研（style-factor-research-2026.md）称质量 2024–2026 走强、与价值互补；本验证在
成分股 + 月频 IC 口径下未见显著 alpha。差异来源：①调研样本为券商/中证全 A 多空
组合口径，本验证为 300+500 多头横截面；②"华证新质量"为多维度合成（定价能力+资本
效率+市场地位+财务真实性），本验证为单维 ROE 代理；③月频 IC 对财报因子的低频
特性不敏感。后续可尝试：TTM ROE 口径、合成多维质量、季度调仓 IC。


## 5. 多维质量因子扩展验证（2026-08-13 第二轮）

补 QMJ/华证体系 4 个新 raw 因子（`stockfu/factors/raw/quality.py`，全部年报口径避免
季度季节性）：`gpoa`（毛利/总资产，Novy-Marx）、`net_margin`（净利率）、
`cash_quality`（经营现金流/净利，Sloan 盈余质量）、`asset_growth`（总资产同比，
**负向**）。字段级 PIT：跨表因子要求各来源表公告日均已可见（实测 balance 公告日
可能晚于 profit，保守缺失不拼凑）。

### 5.1 单因子 IC（2016-2026 全期，月频 h=21）

| 因子 | IC | t | 缺失率 | Q1-Q5 多空 |
|---|---|---|---|---|
| gpoa | 1.05% | 0.83 | 15.8% | +0.06（前 4 桶单调 0.40） |
| net_margin | 0.48% | 0.49 | 10.0% | -0.46 |
| cash_quality | 0.28% | 0.38 | 19.2% | -0.19 |
| asset_growth | -0.40% | -0.40 | 11.4% | +0.06（负向方向对） |

全部 |t|<1：**多维化未改变"质量因子在成分股月频 IC 下无显著 alpha"的结论**。

### 5.2 合成质量策略回测（六维：quality_roe 25% + gpoa 20% + net_margin 20%
   + cash_quality 15% + asset_growth 10% + leverage 10%）

| 策略 | 年化 | 超额 | 回撤 | Sharpe |
|---|---|---|---|---|
| quality_v1（纯 ROE 质量） | 5.31% | 14.8% | 57.3% | 0.34 |
| **quality_multi_v1（六维合成）** | 4.82% | 8.3% | **46.7%** | 0.32 |
| multi_factor_v2（对照） | 7.34% | 44.8% | 28.9% | 0.53 |
| multi_factor_quality_v2 | 9.19% | 76.4% | 32.0% | 0.61 |

### 5.3 最终结论

1. **质量（单维或六维合成）单独成策略均不可行**：年化 ~5%、回撤 47-57%。
2. **质量作为多因子一极的增益是唯一有效用法**（multi_factor_quality_v2）。
3. 原因推测：300+500 成分股内质量差异已被指数筛选压缩；质量是慢变量，月频+21 日
   前向窗口不匹配（可能需要季度调仓/更长持有验证）。
4. 建议：质量维度保留在 multi_factor_quality_v2（待三段门禁正式验证）；
   quality_multi_v1 / quality_v1 作为研究配置保留，不进入正式保留集。

### 5.4 本轮产物

- raw：gpoa/net_margin/cash_quality/asset_growth（已注册 RAW_COMPUTERS）
- profiles：gpoa_v1 / net_margin_v1 / cash_quality_v1 / asset_growth_v1
- alpha：quality_multi_v1（六维合成）
- 回测 run_id：quality_multi_v1=`b3be0bb7`
- 测试：test_financial_quality.py 14 项（含跨表 PIT 字段级可见性）

## 产物

- `scripts/quality_factor_ic.py`（IC 研究脚本，复用 factor_diag 纯函数 + 财务 provider）
- 回测 run_id：multi_factor_quality_v2=`a5b9537a`，multi_factor_v2=`97753e47`，quality_v1=`cb469315`
