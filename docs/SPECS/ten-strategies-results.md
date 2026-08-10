# 10 策略回测结果（V2 引擎，2026-08-09）

> 配套 `docs/SPECS/ten-strategies-research.md`（策略来源）与 `data/backtest/ten-strategies/summary.md`（自动生成的对比表）。

## 口径

- 窗口:2021-01-01 → 2026-08-04(qfq 估值)、预热 history_origin 2018-01-01、固定观察期 271 日。
- 股票池:沪深300 历史点时成分并集按日过滤(`--codes hs300`)。
- 复用只读数据快照 `data/snapshots/stockfu-2ee50075f50c.db`(内容 SHA 与库数据 max 2026-08-04 一致)。
- 基准:沪深300,区间收益 **−1.08%**。
- 资金 1,000,000、T+1 开盘执行、涨跌停/停牌/整手/费用/滑点均按引擎主线;均为 **long-only**(A股融券受限)。
- **数字为研究回测结果,用于风格对比与因子诊断,不代表策略优劣或实盘收益。** 这张表是历史的单窗口结果；正式结论必须按 `full`、`2013-2019`、`2020-2026` 三段门禁运行。

## 结果对比表

| # | 策略 | 因子 | 调仓 | 风险 | 总收益 | 年化 | 最大回撤 | Sharpe | 超额 | 成交 |
|---|---|---|---|---|---:|---:|---:|---:|---:|---:|
| 10 | 多因子 | E/P+B/P+动量+低波 | 月 | no_overlay | **+74.46%** | **+13.84%** | 18.23% | **0.94** | +75.54% | 843 |
| 2 | 价值 | E/P + B/P | 月 | no_overlay | +49.71% | +9.85% | 17.62% | 0.69 | +50.79% | 575 |
| 1 | 高股息红利 | TTM 股息率 | 月 | no_overlay | +40.51% | +8.24% | 18.43% | 0.54 | +41.59% | 614 |
| 8 | 低Beta防御 | 120日β + 低波 | 月 | no_overlay | +31.10% | +6.51% | **14.30%** | 0.58 | +32.18% | 964 |
| 5 | 低波动 | 总波 + 下行波 | 月 | no_overlay | +19.12% | +4.16% | 18.38% | 0.39 | +20.20% | 1059 |
| 9 | 52周新高 | close/250日高点 | 月 | no_overlay | −5.84% | −1.39% | 28.11% | 0.03 | −4.76% | 1271 |
| 3 | 横截面动量 | 12-1月收益 | 月 | no_overlay | −14.03% | −3.46% | 39.84% | −0.00 | −12.95% | 866 |
| 6 | 趋势跟踪 | 趋势r²+动量 | 周 | trailing+regime | −24.54% | −6.35% | 29.99% | −0.33 | −23.46% | 3805 |
| 4 | 短期反转 | 20日收益反向 | 周 | no_overlay | −31.04% | −8.29% | 48.96% | −0.24 | −29.96% | 4350 |
| 7 | RSI均值回归 | RSI14 | 日 | no_overlay | −64.22% | −21.29% | 67.25% | −1.09 | −63.14% | 8938 |

基准沪深300:−1.08%。趋势策略 `trend_trailing_v2` 风控在 smoke 中触发(regime 96 次、take_profit 1 次);本全周期 formal 段触发记录见各日志 `risk_metrics`。

## 结果解读（风格分化与文献印证）

这段窗口(2021 初 A 股见顶 → 2024 熊 → 2025 反弹)以**防御/价值风格占优**为特征,结果与文献预测高度一致:

- **防御簇胜出**(价值/红利/低波/低β):熊市中"越跌越便宜 + 低波动 + 低 beta"组合显著跑赢基准。
  多因子(AQR 风格分散)最优(+74%,Sharpe 0.94)印证"单因子分散化降低风险、提升夏普"的文献结论。
- **动量崩溃**(`momentum_jt` −14%、Sharpe≈0):2021–2024 熊市正是 Jegadeesh-Titman 记载的
  **动量崩溃(momentum crash)** 高发场景(市场急跌反弹期);且 A 股短期动量本就弱于美股(研究 §3)。
- **RSI/反转"接飞刀"**(`rsi_reversal` −64%、`reversal` −31%):日/周度高频换仓(8938/4350 笔)
  在单边下跌中持续买入下跌股,正是研究 §4/§7 警示的"超卖可更超卖/接飞刀",换手成本也被放大。
- **52 周新高**(−5.84%):George-Hwang 锚定效应在 A 股反弹期投机股主导下被削弱(研究 §9 警示)。
- **低Beta防御回撤最低**(14.30%):与 AQR "Understanding Defensive Equity" 的"defensive 组合最大回撤显著小于基准"一致。

**结论**:10 个策略风格各异、收益与回撤分化明显,引擎如实反映了各风格在该市场状态下的行为;
亏损策略并非"实现错误",而是其风格在 2021–2024 A 股熊市中的真实暴露(文献已预告)。
任何策略是否"可用"须按 §0.6.6 三段门禁验证；不能用这张 2021–2026 单窗口表替代。

## 复现

```bash
cd /opt/pro/stockfu
python3 main.py --backtest-v2 <alpha_id> \
  --start 2021-01-01 --end 2026-08-04 --history-origin 2018-01-01 \
  --observation-count 271 --codes hs300 \
  --snapshot data/snapshots/stockfu-2ee50075f50c.db
# alpha_id 任选:dividend_income_v2 value_ep_bp_v2 momentum_jt_v2 reversal_jl_v2
#   low_volatility_pure_v2 trend_following_v2 rsi_reversal_v2
#   defensive_low_beta_v2 fifty_two_week_high_v2 multi_factor_v2

# 正式三段（固定 full/2013-2019/2020-2026）
python3 main.py --backtest-v2-segments <alpha_id> \
  --snapshot data/snapshots/stockfu-2ee50075f50c.db \
  --codes hs300 --observation-count 271 --canonical
```

## 已知限制 / 后续

- 本文件的主表是单窗口(2021–2026,偏熊市)历史结果；三段正式产物写入 `data/backtest/v2-segments/run-*/`，完整规则见 `docs/BACKTEST.md` §0.3.1/§0.6.6。
- 当前行情库从 2013 年起；尚不能把三段结果称为 2007–2026 全样本门禁。
- 多因子缺 Quality 维度(库内无 ROE/毛利等基本面数据);趋势用 signed_r²+动量代理(无 Donchian/MA 交叉算子),风险层忠实"截断亏损/追踪止盈/MA200 regime"。
- 内存:`multi_factor_v2`(4 因子)峰值 RSS ~854MB,需单独运行(2 并发会与其它进程争内存触发 OOM);其余 9 个 2 并发可跑。
- 全为 long-only(融券受限);因子收益是多头腿,不等同学术 long-short 因子溢价。
