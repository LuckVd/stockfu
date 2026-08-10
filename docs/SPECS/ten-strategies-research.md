# 10 个主流量化策略 · 权威规则研究汇编（2026-08）

本文件是「在 V2 回测系统实现 10 个风格各异策略」任务的策略来源依据，供 `configs/alphas/`
下 10 个 alpha 配置对照。每条规则的算子/窗口/调仓/止损止盈均贴合主流学术与实务定义；
实现中因数据可得性或引擎能力做出的偏差，在文末「实现偏差」列出。

## 汇总表（对照实现）

| # | 策略(alpha_id) | 主要因子 | 回看窗 | 调仓 | 止损/止盈 | 风险政策 |
|---|---|---|---|---|---|---|
| 1 | 高股息 `dividend_income_v2` | TTM 现金股息率 DPS/P | 过去 12 个月分红 | 月度 | 学术版无 | no_overlay |
| 2 | 价值 `value_ep_bp_v2` | E/P + B/P(HML) | TTM PE/PB | 月度 | 学术版无 | no_overlay |
| 3 | 横截面动量 `momentum_jt_v2` | 12-1 月累计收益 | [t-252, t-21] | 月度 | 学术版无 | no_overlay |
| 4 | 短期反转 `reversal_jl_v2` | 近 20 日收益(反向) | 20 交易日 | 周度 | 学术版无 | no_overlay |
| 5 | 低波动 `low_volatility_pure_v2` | 总波动 + 下行波动 | 20/60 日年化 | 月度 | 学术版无 | no_overlay |
| 6 | 趋势跟踪 `trend_following_v2` | 趋势线性度(signed r²)+动量 | 60 日/12 月 | 周度 | **有**:trailing 止盈+10%止损+MA200 regime | trend_trailing_v2 |
| 7 | RSI 均值回归 `rsi_reversal_v2` | RSI(14) | 14 日 | 日度 | 信号驱动退出(RSI 回升即卖) | no_overlay |
| 8 | 低 Beta 防御 `defensive_low_beta_v2` | 120 日 beta + 低波 | 120/20 日 | 月度 | 学术版无 | no_overlay |
| 9 | 52 周新高 `fifty_two_week_high_v2` | P / 近 250 日最高收盘 | 250 日 | 月度 | 学术版无 | no_overlay |
| 10 | 多因子 `multi_factor_v2` | E/P+B/P+动量+低波 | 同上 | 月度 | 学术版无 | no_overlay |

## 权威来源（每个策略）

1. **高股息**:O'Higgins & Downes (1991) *Beating the Dow*；Fama-French (1988) JFE 22(1)；
   Kenneth French Data Library（D/P 口径）。学术因子年度重组(6 月末)，A股实务月度。
   **无止损止盈**——价值/红利因子越跌越便宜，加止损反向破坏逻辑。
2. **价值**:Fama-French (1992) JF 47(2)；(1993) JFE 33(1) HML 构造。
   B/M = 财年 t-1 账面权益 / Dec(t-1) 市值（6 月会计滞后）；E/P 类似。30/40/30 分位、市值加权。
   **无止损止盈**。
3. **横截面动量**:Jegadeesh-Titman (1993) JF 48(1)；Carhart (1997) PR1YR。
   J=12 形成期剔除最近 1 月(12-1)，日频 ≈ [t-252, t-21]。月度调仓，K>1 用重叠组合。
   **无止损止盈**（已知存在动量崩溃风险，可选市场状态过滤）。A股短期动量弱、反转强。
4. **短期反转**:Lehmann (1990) QJE 105(1) 周反转；Jegadeesh (1990) JF 45(3) 月反转；
   Fama-French ST_Rev。排序变量=过去 1 月(≈21 日)收益，方向与动量相反(买输家)。
   **无止损止盈**；A股反转效应显著(优于美股)。
5. **低波动/BAB**:Frazzini-Pedersen (2014) JFE 111(1) BAB；Ang et al. (2006) JF 61(1) IVOL；
   Clarke-de Silva-Thorley (2011) 最小方差。BAB 月度；低波月度/季度。
   **无止损止盈**。A股低波溢价显著(Robeco)。
6. **趋势跟踪**:Faber (2007) MA200；Original Turtle Trading Rules（Donchian 20/55 + 2N 止损）；
   Curtis Faith (2007)。趋势系统核心:**截断亏损、让利润奔跑**——2×ATR(2N) 止损、
   2% 账户风险、反向通道出场；MA200 作风险开关(规避熊市)。日度监控。
7. **RSI 均值回归**:Wilder (1978) RSI 14，70 超买/30 超卖；Connors RSI(2)。
   入场 RSI<30(超卖)，出场 RSI>50 或 >70(反弹兑现)。日度。原版常不设固定止损，
   可选时间止损(5-10 日)或入场−2×ATR。
8. **低 Beta 防御**:Black (1972) JB 45(3)；Frazzini-Pedersen (2014) BAB；
   AQR "Understanding Defensive Equity" (2012，季度)。低 beta 异象 α=ψ(1−β)。
   beta 估计简化为 120 日 OLS（实务轻量口径）。**无止损止盈**。
9. **52 周新高**:George-Hwang (2004) JF 59(5)；Jeon-Byun (2023) FAJ 79(2)。
   Nearhigh = P / 近 252 日**最高收盘价**，∈[0,1]。月末调仓持有 1 月。
   Nearhigh 预测力主导并优于传统动量。**无止损止盈**。
10. **多因子/QMJ**:Asness-Frazzini-Pedersen (2019) *Quality Minus Junk*；
    Novy-Marx (2013) 毛利溢价；AQR "Combining Value/Momentum/Quality/LowVol"。
    典型等权或 inverse-variance 加权。QMJ 月度。**无止损止盈**；QMJ 危机中 α 为正。

## 实现偏差（已知，需在结果解读时注意）

- **趋势 #6**:V2 引擎无 Donchian 通道/MA 交叉算子，故以「趋势线性度 signed_r² + 12-1 动量」
  作为趋势代理；风险层 `trend_trailing_v2` 忠实落地趋势系统精髓——个股 10% 止损、分段追踪止盈
  (浮盈后按高点回撤卖出，让赢家跑)、沪深300 MA200 regime(大盘破位时压缩敞口)。
- **多因子 #10**:库内无 ROE/毛利等基本面数据（quote_snapshot 仅有 PE/PB），故 Quality 维度
  未纳入，组合为 Value(E/P+B/P)+Momentum+LowVol 三主题，缺 AQR 的 Quality 一极。
- **52 周 #9**:用 qfq 收盘价计算最高收盘（与研究「调整后最高收盘」一致，避免分红除权假跌破）。
- **价值 #2**:用 TTM PE/PB 即时值，未做 Fama-French 的 6 月会计滞后与 NYSE 断点 2×3 构造
  （V2 是 top-N 等权多头，非 long-short 分位）。
- **做空限制**:A股融券受限，10 策略均为 long-only（V2 引擎本就是多头研究系统），
  因子收益是多头腿，不等同学术 long-short 因子溢价。
- **sample 范围**:2007–2026 全样本 + 三跑门禁(§0.6.6)未执行；本次为单一窗口研究回测。
