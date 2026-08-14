# 财务三表 PIT 数据设计（东财 datacenter-web）· 2026-08-13

> 状态：已定稿。数据源从 baostock（按股票×年×季逐次调用，受 5 万/天上限）
> 切换为东方财富 datacenter-web（按报告期一次返回全市场），调用量从 ~82 万次
> 降到 ~2,400 次请求，总耗时约 1–2 小时。
> 来源：2026-08-13 实测验证（接口可用性、字段、NOTICE_DATE、2010Q1 老报告期）。

## 1. 数据源

| 接口 | reportName | 关键字段（原始 JSON 名） |
|---|---|---|
| 业绩报表 | `RPT_LICO_FN_CPD` | `WEIGHTAVG_ROE`(ROE)、`XSMLL`(销售毛利率)、`PARENT_NETPROFIT`(归母净利)、`TOTAL_OPERATE_INCOME`(营业总收入)、`YSTZ`(营收同比)、`SJLTZ`(净利同比)、`BASIC_EPS`、`BPS`(每股净资产)、`MGJYXJJE`(每股经营现金流)、`NOTICE_DATE` |
| 资产负债表 | `RPT_DMSK_FN_BALANCE` | `TOTAL_ASSETS`、`TOTAL_LIABILITIES`、`LIABILITY_TO_ASSET`(资产负债率)、`TOTAL_EQUITY`、`MONETARYFUNDS`、`ACCOUNTS_RECE`(应收)、`INVENTORY`、`ACCOUNTS_PAYE`(应付)、`TOTAL_ASSETS_YOY`、`NOTICE_DATE` |
| 现金流量表 | `RPT_DMSK_FN_CASHFLOW` | `NETCASH_OPERATE`(经营现金流净额)、`NETCASH_INVEST`、`NETCASH_FINANCE`、`NETCASH_TOTAL`、`NOTICE_DATE` |

- 按 `REPORTDATE='YYYY-MM-DD'`（资产负债表为 `REPORT_DATE`）过滤，每报告期分页拉取（pageSize=500）。
- 可用范围：**2010Q1 起**（akshare 文档确认，实测 2010Q1 返回 2,307 只）。
- 返回字段以实测为准，脚本按返回 JSON 动态取值，映射外字段跳过。

## 2. 表结构与关联

### 2.1 关联键

**6 张表→4 张表**（`financial_operation` / `financial_dupont` 无东财按报告期对应接口，2026-08-13 决定不预留、删除）。

4 张表**互不设外键**，全部通过复合键 `(asset_code, year, quarter)` 关联：
同一只股票、同一报告期。`asset_code` 与全库统一（6 位无前缀，如 `600519`），
可直接与 `quote_snapshot` / `dividend_event` 的 `asset_code` join。

```
financial_profit ──┐
financial_growth ──┼── (asset_code, year, quarter) ──► quote_snapshot.asset_code
financial_balance ─┤                                    dividend_event.asset_code
financial_cashflow ┘
```

### 2.2 PIT 时点

每行带 `pub_date`（= `NOTICE_DATE` 公告日）与 `stat_date`（= 报告期）。
回测读取模式：**某交易日 → 该股票在此日前最新已公告的财报**：

```sql
SELECT f.asset_code, f.year, f.quarter, f.roe_avg, f.pub_date
FROM financial_profit f
WHERE f.asset_code = '600519' AND f.pub_date <= '2024-05-10'
ORDER BY f.year DESC, f.quarter DESC LIMIT 1;
```

### 2.3 表字段（东财映射）

| 表 | 字段（snake_case） | 东财源字段 |
|---|---|---|
| `financial_profit` | `roe_avg` / `gp_margin` / `net_profit` / `eps` / `revenue` / `revenue_yoy` / `net_profit_yoy` / `bps` / `cash_per_share` | `WEIGHTAVG_ROE` / `XSMLL` / `PARENT_NETPROFIT` / `BASIC_EPS` / `TOTAL_OPERATE_INCOME` / `YSTZ` / `SJLTZ` / `BPS` / `MGJYXJJE` |
| `financial_growth` | `yoy_ni` / `yoy_asset` / `yoy_equity` | `SJLTZ` / 资产负债表 `TOTAL_ASSETS_YOY` / 股东权益同比（自算，暂留空） |
| `financial_balance` | `total_assets` / `total_liabilities` / `liability_to_asset` / `equity` / `monetary_fund` / `receivables` / `inventory` / `payable` / `total_assets_yoy` | `TOTAL_ASSETS` / `TOTAL_LIABILITIES` / `LIABILITY_TO_ASSET` / `TOTAL_EQUITY` / `MONETARYFUNDS` / `ACCOUNTS_RECE` / `INVENTORY` / `ACCOUNTS_PAYE` / `TOTAL_ASSETS_YOY` |
| `financial_cashflow` | `net_cash_oper` / `net_cash_inv` / `net_cash_fin` / `net_cash_total` | `NETCASH_OPERATE` / `NETCASH_INVEST` / `NETCASH_FINANCE` / `NETCASH_TOTAL` |

公共列（全部表）：`id, asset_code, year, quarter, pub_date, stat_date, source, updated_at`；
唯一约束 `(asset_code, year, quarter)`；索引 `asset_code` / `pub_date`。

## 3. 落库范围

**全市场入库**（东财一次请求即全市场，落库过滤 0/3/6 开头的 A 股）：
约 66 报告期 × 平均 ~3,800 只 × 3 表 ≈ **75 万行 / 150–200MB**（现有库 1.7GB，+10%）。
将来扩展股票池（全 A 策略、荐股自选股）无需重新拉取。

## 4. 回补机制

- checkpoint：`backfill_checkpoint`，`task_key="financial-em-v1"`，`scope_key=接口名`，`item_key=报告期(YYYY-MM-DD)`；每报告期完成后标记，断点续传。
- 限流：分页请求间隔 0.3–0.5s（东财 datacenter-web 实测宽松；push2/push2his 仍封死，不涉及）。
- 每日配额防呆不再需要（东财无 5 万/天限制），删除 baostock 版配额逻辑。
- 失败重试：单页失败重试 2 次，报告期失败留 failed 待下次续跑。

## 5. 已知缺口（记录在案，不阻塞）

1. `financial_operation`（周转率）/ `financial_dupont`（ROE 分解）：东财无按报告期全市场接口，**已决定不预留**；需要时按需从 baostock 对目标股票补。
2. 退市股历史报告期：东财按报告期快照，退市股早期数据可能缺失（baostock 按股票查可覆盖）；成分股中退市股极少，暂不处理。
3. `yoy_equity`（股东权益同比）：需要跨报告期自算，暂留空，因子层按需计算。
4. 部分指标如流动比率/速动比率（需流动负债明细）：东财资产负债表无流动负债单列，留空。
5. ~~2025Q4 报告期部分公司数值异常~~ — **2026-08-13 已联网核实：非异常，系真实业绩下滑**。
   约 148 家公司 2024 年报 ROE>10% 而 2025 年报 ROE 腰斩；以五粮液（23.4→6.9、
   营收 891→405 亿）为例，与深交所官方年报逐项一致（-54.55% / -71.89% / ROE 减少
   16.46pct）：白酒行业 2025 年深度调整（产量 -12.1%、收入 -7.5%、利润 -13.3%），
   公司董事长被留置、年报延期并对前期财报重大会计差错更正（前三季营收下修 303 亿）。
   因子 PIT 行为正确：2026-04-30 公告日后质量确实断崖下降。引用 2025 年报结论属真实
   基本面，无需复检；唯 2025Q1-Q3 历史季度数据可能存在更正前后口径差异（东财是否
   同步更正未验证），季度口径因子（若有）需注意。

## 6. 质量因子 raw 接入（2026-08-13 实现）

`stockfu/factors/raw/quality.py`，PIT 统一走 `stockfu/services/financial.py`：

| raw_metric_id | 计算 | 口径 |
|---|---|---|
| `quality_roe` | 最新完整年度 ROE − pstdev(近 5 个年度 ROE)，不足 3 个年度退化纯水平 | 全部年报（quarter=4），避免单季/年度混用季节性偏误 |
| `gross_margin` | 最新已公告报告期销售毛利率 XSMLL | 可负（真实信息），银行/券商等无此字段 → 缺失 |
| `leverage` | 最新已公告报告期资产负债率 LIABILITY_TO_ASSET | ≤0 视为异常缺失；>100（资不抵债）保留原值 |

- 回测性能：`backtest.engine._preload_financial_reports` 一次 SQL 预载宇宙财务行，
  `_backtest_series_ctx` 挂 `financial provider` 按日 bisect 切片，零逐票查库；
  v2 引擎默认挂载，旧引擎/未预载路径回落 DB（语义等价，测试盯）。
- 设计依据：docs/SPECS/style-factor-research-2026.md（简单 ROE 弱、稳定性改进版才显著）。

## 7. 入口

```bash
python3 main.py --backfill-financial                 # 全量（2010Q1 起，约 1-2 小时）
python3 main.py --backfill-financial --fin-reports 20240331,20240630   # 指定报告期
python3 main.py --backfill-financial --fin-status    # 进度统计
```
