# stockfu 项目状态（工作日志 / 冷启动手册）

> 新会话先读这份，就能接上。项目：`/opt/pro/stockfu/`。
> 定位：**StockFu·资产管理终端**，借鉴 `../daily_stock_analysis` 的多数据源 fallback 思想，**Web（FastAPI+Vue3）+ CLI**；TUI 已移除。

## 1. 一句话现状
本地优先的综合资产管理 + 市场情绪终端：持仓、股息/网格、三层 fear/greed/heat、历史回补、AI 4 顾问、**天级回测四层架构**（算子→策略→rebalancer→执行；防未来函数）。SQLite + FastAPI + Vue3。

> **【2026-07-21 口径修正】** 此前全周期总表/选股/荐股数字跑在**混复权**行情上，**全部作废**。行情已统一前复权；股息率分母改为 **不复权 `close_raw`**（名义现金 ÷ 全样本 qfq 会虚高并引入前视）。`close_raw` 回补已完成（2026-07-22，100%）；红利策略 `--update-backtests` 重跑中，完成后重写 §0.6。

## 0. 进行中任务 / 冷启动接棒（2026-07-22）

> 🚧 **【当前任务】策略参数变体（一等）+ 回测指标持久化** —— 方案已定、待执行。详见 **`docs/STRATEGY_VARIANTS_PLAN.md`**（自包含、逐行验证）。工作区：`/opt/pro/stockfu-backtest` 新分支 `feature/strategy-variants`。两块：A) `strategy_id` 编码变体（`base#key`，seed 展开器）；B) 引擎原生产出 回本/买入/止损成交/止损损失/水下分布(0/10/20/30%) 并自动入 `.json.gz`+`.meta.json`。首个用例：`dividend_cross_section`(8%) + `dividend_cross_section#sl30`(30%) 并存。
>
> **干净回测 16/16 已落盘**（§0.6 全表，带回撤/卡玛/回本/换手/磨损/仓位/止损成交/止损损失/胜率）：12 策略先落盘 + 本次提速重启 4（pure_factor / dual_bollinger / bollinger_reversion / bollinger_reversion_cross_section，07-22 18:11 完成）。
> **【2026-07-22 · 回测性能修复】** ① bollinger 的行情 N+1 已改为日期预载；② value 的多年 PE/PB 分位已改为 5 年内存预载；③ value 参数指纹已对齐，复用其他策略缓存；④ MACD 周线、TTM 分红事件及 `close_raw` 分母均已补入预载，数学算子热路径不再逐 `(code,as_of)` 查询数据库。
> **剩余 4 策略已完成**（07-22 18:11:59，`ok:4 fail:0`）：PID `2880236` 已退出，4 产物全部落盘；§0.6 已补满为 16/16 全表（含止损成交/止损损失/胜率）。

### 0.1 三复权 baostock 串行回补（✅ 已完成，留存复跑参考）

> **2026-07-22 已完成**：`ok=214 fail=0`，`raw_pct`/`hfq_pct`=100%，耗时 2030s，代理池 rotates=7 / dropped=6。以下为复跑/排错参考。

| 项 | 值 |
|----|-----|
| 命令 | `python3 main.py --backfill-adj-prices --start 2020-01-01 --end 2026-07-20` |
| 代理 | **默认 `--proxy-mode free`**：启动拉公网免费代理入池 + 本机 Clash `7891` 种子；单 IP 串行；失败/黑名单立即剔除并切换 |
| 其它模式 | `--proxy-mode clash` 仅本机 SOCKS；`--proxy-mode direct` / `--no-socks` 直连 |
| 池自愈 | 耗尽自动重拉(`BAOSTOCK_REBOOTSTRAP_MIN_INTERVAL`/`_MAX`)；死 IP TTL 复活(`BAOSTOCK_DEAD_TTL`，默认 1800s)；常驻通道 `maybe_refresh`(`BAOSTOCK_MAX_AGE`/`BAOSTOCK_MIN_ALIVE`)——长回补/web/`--schedule` 不再因池薄中断；**查询超时换 IP**(`BAOSTOCK_FETCH_TIMEOUT` 默认 60s，防 login 通过却卡在内部接收循环的坏代理) |
| 直连兜底 | 代理池+rebootstrap 全耗尽→自动直连 baostock(`BAOSTOCK_DIRECT_FALLBACK` 默认 on,IP 解封可用;`_MAX`/`_COOLDOWN` 限流防再被封硬撞);`maybe_refresh` 池回血后切回。**A 股 `--fetch` 同走此链**(三复权+全字段+当日 `close_raw`),baostock 全失败即放弃、**不降级东财/腾讯**(残缺数据冒充完整);ETF→akshare、港美股→yfinance |
| 源 | **仅 baostock**；`preserve_qfq=True`（只补 raw/hfq）。源经本机 clash `7890` 拉(`BAOSTOCK_SOURCE_PROXY=auto`，GitHub 列表国内可达)；可选 `data/proxy_sources.json` 外置合并私有/付费镜像 |
| 断点续传 | **默认开**：跳过 `[start,end]` 内 raw/hfq 已覆盖 qfq 的 code（一条分组聚合判定），只补缺口；`--full` 强制全量重抓。重跑只做未完成的部分，不再全量重抓 |
| 实现 | `stockfu/data/free_proxy_pool.py` + `baostock_proxy.py`；HTTP CONNECT / SOCKS 隧道 |
| 日志建议 | `nohup python3 -u main.py --backfill-adj-prices --start 2020-01-01 --end 2026-07-20 > data/backfill_adj_prices_pool.log 2>&1 &` |
| 覆盖率 | `python3 -c "from stockfu.scheduler.backfill_adj_prices import adj_price_coverage; print(adj_price_coverage())"` |
| 完成标志 | 日志出现 `=== 完成 ok=…`；`raw_pct`/`hfq_pct` 接近 100% |

> IP 已解封,代理池仍为首选;直连仅作代理池耗尽兜底(`BAOSTOCK_DIRECT_FALLBACK`,自动触发)。勿手动 `--proxy-mode direct` 跑全量(串行单 IP 风险)。进程可 double-fork 后台跑。

若进程挂了（关会话后应仍在；若 `ps` 无进程）：

```bash
cd /opt/pro/stockfu
python3 - <<'PY'
import os, sys
from pathlib import Path
os.chdir('/opt/pro/stockfu')
if os.fork()>0: sys.exit(0)
os.setsid()
if os.fork()>0: sys.exit(0)
Path('data/backfill_adj_prices.pid').write_text(str(os.getpid())+'\n')
so=open('data/backfill_adj_prices_pool.log','ab')
os.dup2(open('/dev/null','rb').fileno(),0); os.dup2(so.fileno(),1); os.dup2(so.fileno(),2)
os.execvp('python3',['python3','-u','main.py','--backfill-adj-prices','--start','2020-01-01','--end','2026-07-20'])
PY
```

### 0.2 下一步

> ✅ **16/16 干净结果已落盘**（§0.6 全表，07-22 18:11 完成，`ok:4 fail:0`）。PID `2880236` 已退出，无残留进程。

1. 可选：`--recommend` 在干净缓存上重跑选股。
2. 可选：对证伪的 5 个策略（`macd_cross` / `bollinger_reversion` / `bollinger_reversion_cross_section` / `dividend_low_vol` / `pure_factor`）评估下线或调参。

### 0.3 代码已推送（2026-07-21）

`origin/main` 含：去 sina/pytdx、全周期/CS 策略族、三复权 schema+串行回补、股息率 raw 分母、冷启动文档清理。

### 0.4 荐股（CLI 已固化）

```bash
python3 main.py --recommend --strategies cross_section_factor,reversal_cross_section \
  --as-of 2026-07-17 --cash 1000000
# 产物 data/reports/recommend/…（runtime，gitignore）
```

### 0.5 已完成（代码层，已 push）

| 项 | 说明 |
|----|------|
| 去 TUI | 无参 `main.py` = Web |
| 全周期更新 | `full_cycle_update.py` + `--update-backtests` / `--list-strategies` |
| 横截面策略族 | CS 孪生 + 红利/反转/ETF 动量等；`cap_and_rank` / `top_n_picker` |
| 新算子 | `reversal` / `low_volatility` / `dividend_yield` |
| 回测性能 | 区间预载 D/E、quote_series 内存供给器、冷启动批量写缓存 |
| 三复权 schema | `quote_snapshot`：`*_qfq`（遗留 open/high/low/close ≡ qfq）、`*_raw`、`*_hfq` |
| 股息率口径 | `dividend_yield_ttm` 分母 **close_raw**；算子 `price_basis=raw` |
| 数据源 | 删 sina/pytdx；K 线路径强制前复权 |
| baostock 直连兜底 + fetch 统一 | 代理池+rebootstrap 耗尽→直连(`BAOSTOCK_DIRECT_FALLBACK` 默认 on);A 股 `--fetch` 走 baostock 三复权(全字段+当日 `close_raw`),失败不降级;ETF→akshare、港美股→yfinance。**✅ 已端到端验证(2026-07-22)**:人为清空代理池+禁自愈,兜底 ON→直连接住、三复权 qfq/raw/hfq 拉满(`proxy=direct`);OFF→旧行为抛异常中止 |

### 0.6 全周期结果总表（2026-07-22 干净重跑，16/16）

> 区间 2021-01-04 → 2026-07-20（1342 交易日），初始资金 100 万，前复权，股息率分母 `close_raw`，基准上证 +8.37%。按年化降序。**勿引用 07-19/07-20 混复权 / qfq 分母旧表。**

| # | 策略 | 基本原理 | 涨跌% | 年化% | 最大回撤% | 回本(交易日) | 买入个数 | 日均换手% | 磨损费(万) | 平均仓位% | 夏普 | 止损成交 | 止损损失(万) | 胜率% |
|---|------|---------|------:|------:|---------:|-----------:|--------:|---------:|----------:|----------:|-----:|--------:|-----------:|-----:|
| 1 | `momentum_breakout` | 月线动量突破:月布林+动量 | **+106.3** | **+14.56** | 33.59 | 174 | 492 | 0.76 | 15.0 | 70.9 | +0.58 | 330 | 203.8 | 41.6 |
| 2 | `momentum_breakout_cross_section` | 动量突破横截面:月布林+动量+趋势 | **+90.2** | **+12.83** | 38.45 | 318 | 713 | 1.89 | 14.0 | 72.4 | +0.58 | 795 | 232.8 | 39.4 |
| 3 | `dividend_cross_section` | 红利横截面:高股息+低波+价值 | **+50.6** | **+7.99** | 14.16 | 356 | 196 | 0.20 | 3.1 | 92.0 | +0.63 | 171 | 60.6 | 47.4 |
| 4 | `cn_momentum_cross_section` | 个股动量横截面:动量+线性度+趋势 | +27.9 | +4.72 | 43.38 | 319 | 731 | 1.74 | 9.5 | 77.8 | +0.31 | 699 | 150.8 | 37.5 |
| 5 | `dual_bollinger` | 周+月双布林趋势+动量 | +25.9 | +4.43 | 42.57 | 416 | 566 | 0.89 | 17.5 | 93.4 | +0.30 | 408 | 212.4 | 42.6 |
| 6 | `etf_momentum_cross_section` | ETF动量横截面:动量+线性度+趋势 | +20.9 | +3.63 | 19.14 | 249 | 27 | 0.58 | 5.0 | 39.1 | +0.37 | 10 | 3.7 | 40.9 |
| 7 | `etf_momentum_rotation` | ETF动量轮动 | +15.0 | +2.65 | 21.55 | 691 | 27 | 0.15 | 3.6 | 24.0 | +0.31 | 7 | 7.2 | 41.2 |
| 8 | `cn_momentum_rotation` | 动量轮动(降换手)+止损 | +12.4 | +2.22 | 45.56 | 319 | 573 | 0.88 | 8.6 | 67.3 | +0.21 | 284 | 79.9 | 38.7 |
| 9 | `reversal_strategy` | 反转均值回归:短期反转+RSI超卖+低估 | +8.6 | +1.55 | 36.73 | 231 | 505 | 0.91 | 10.0 | 66.7 | +0.18 | 466 | 166.5 | 51.6 |
| 10 | `cross_section_factor` | 多因子横截面:反转+低波+价值 | +6.3 | +1.15 | 37.78 | 344 | 632 | 1.78 | 9.7 | 82.5 | +0.15 | 1237 | 222.0 | 49.6 |
| 11 | `reversal_cross_section` | 反转横截面:短期反转+RSI+低估 | +5.0 | +0.93 | 37.56 | 244 | 680 | 1.95 | 11.2 | 84.4 | +0.15 | 1217 | 251.1 | 52.9 |
| 12 | `pure_factor` | 纯因子:动量+反转+趋势+价值 | +4.8 | +0.88 | 55.16 | 未回本 | 501 | 0.91 | 11.8 | 71.7 | +0.17 | 453 | 137.3 | 42.3 |
| 13 | ❌ `bollinger_reversion_cross_section` | 布林回归横截面:日+周布林+RSI | -6.2 | -1.19 | 38.45 | 465 | 772 | 2.36 | 12.3 | 89.8 | +0.01 | 1088 | 177.3 | 50.0 |
| 14 | ❌ `dividend_low_vol` | 红利低波:高股息+低波动+低估 | -8.9 | -1.73 | 34.44 | 未回本 | 254 | 0.71 | 10.7 | 67.2 | -0.10 | 201 | 69.6 | 49.8 |
| 15 | ❌ `bollinger_reversion` | 日/周布林均值回归:触下轨买、回中上轨卖 | -12.9 | -2.55 | 39.71 | 未回本 | 610 | 0.97 | 17.5 | 79.1 | -0.08 | 324 | 93.5 | 51.4 |
| 16 | ❌ `macd_cross` | MACD金叉买/死叉卖 | -14.4 | -2.88 | 37.99 | 未回本 | 220 | 0.93 | 14.5 | 44.2 | -0.22 | 47 | 7.5 | 41.7 |

> **口径**：买入个数＝去重后曾买入的不同股票数；回本(交易日)＝最大回撤谷底到净值收回前高，`未回本`＝至期末仍水下；止损成交/损失＝`stop_loss` 计划次日(D+1~D+3)成交的平仓单笔数与已实现亏损之和（正值＝亏掉的钱，不含手续费，手续费见「磨损费」）；磨损费＝累计手续费；平均仓位＝平均总多头占用；胜率＝平仓单中 pnl>0 占比。
>
> **风险调整看卡玛**：`dividend_cross_section`（卡玛 0.56）远胜动量双雄（0.33–0.43）——收益高且回撤仅 14%，适合核心仓位；动量双雄收益高但回撤 34–38%、+70% 集中在 2025 顺风年，宜作卫星。`momentum_breakout` 分年：2022 −14% / 2024 +10% / **2025 +70%** / 2026 +15%。
>
> **止损视角**：动量类止损损失最重（动量突破横截面 232.8 万 / 月线突破 203.8 万）但靠趋势仍最赚——止损是「截断亏损让利润奔跑」的成本；反转横截面(251 万)/横截面多因子(222 万)止损损失最大且策略本身不赚钱，止损无效纯磨损。ETF 类 / MACD 止损极少(<8 万)。**胜率高 ≠ 赚钱**：反转类胜率 50%+ 却跑输（小赚多次大亏一次），动量类 37–42% 反而赚大钱。
>
> ❌ **证伪**（跑输基准且夏普 ≤0）：`macd_cross` / `bollinger_reversion` / `bollinger_reversion_cross_section` / `dividend_low_vol` / `pure_factor`（纯因子回撤 55% 全场最高且未回本）。

### 0.7 关键 CLI

```bash
python3 main.py --init-db
python3 main.py --serve
python3 main.py --backfill-adj-prices --start 2020-01-01 --end 2026-07-20   # baostock 串行+免费代理池(断点续传)
python3 main.py --clear-dividend-cache
python3 main.py --update-backtests --start 2021-01-01
python3 main.py --list-strategies
python3 main.py --recommend --strategies cross_section_factor --as-of 2026-07-17
```

**踩坑**：① 并行回测易 `database is locked`；② baostock 裸 TCP 不认 HTTP_PROXY，封禁时走 SOCKS；③ 官方行情 backfill **串行**；④ `--fetch` 只刷自选。

### 0.8 回测慢/卡排查（2026-07-22 踩坑）

某策略 `--update-backtests` 卡住（单策略 >10min、标 warm 却不动）时，几乎都是算子**重复冷补**：

1. **算子 N+1 查库**：算子或其服务层逐 `(code,as_of)` 查行情/估值/分红，会造成百万级 session。**修法已完整落地**：价格与周/月线走 `quote_series` / `quote_series_dates` 预载；value 走 PE/PB 预载；MACD 周线走日期预载；TTM 分红事件与 `close_raw` 分母走预载。`rg 'session_scope|select\\(' stockfu/ai/operators/factors` 应为空；非回测、预载未覆盖或 hfq 保留安全 DB 回退。
2. **params 指纹分裂**：yaml 里 params 写法不一致（如 `{}` vs `{years:5}`），`compute_fingerprint` 不填默认值 → 指纹不同 → 查不到其他策略缓存、独立 N+1 冷补。**查法**：`sqlite3 data/stockfu.db "SELECT fingerprint,COUNT(*) FROM operator_result WHERE operator_id='X' GROUP BY 1;"`（≥2 指纹即分裂）。**修法**：对齐 yaml params + `DELETE` 错指纹。
3. **进度定位**：回测不打印 per-日进度；查 `operator_result WHERE operator_id='X'` 的 `MAX(as_of)` 反推冷补到哪（满 1342 交易日）。注意 `immutable=1` 只读模式在回测写 WAL 时会报 `malformed`，用普通模式。

## 2. 架构（摘要）

```
stockfu/
├── main.py
├── stockfu/
│   ├── models.py          # QuoteSnapshot 三套 OHLC + 状态/估值
│   ├── data/              # baostock/efinance/akshare/tencent/yfinance（无 sina/pytdx）
│   ├── services/          # factors(adj=qfq|raw|hfq) / dividend(raw 分母) / recommend
│   ├── ai/operators/      # math 算子 + strategies yaml
│   ├── backtest/          # engine + scheduler + full_cycle_update + factor_diag
│   ├── scheduler/         # jobs + backfill_adj_prices
│   └── api/ + web/ + frontend/
└── data/stockfu.db
```

## 3. 已完成功能（稳定能力）

- 持仓 / TTM 股息 / 网格；三层情绪；历史回补；Web 看板；AI 4 顾问
- 回测四层 + 算子缓存 + 因子诊断 + 时点宇宙/可成交 + 基准上证
- 全周期批跑 CLI；荐股服务；三复权字段与串行 baostock 回补入口

## 8. 待办

1. ✅ `close_raw` 覆盖完成 + **12/16 干净结果**（§0.6）。剩 4 策略串行运行中，完成后补满全表。
2. ✅ `--fetch` A 股走 baostock 三复权,当日 `close_raw` 自动刷新(无需单独刷)
3. ✅ 回测性能修复（bollinger N+1 + value 指纹，§0.8）
4. 小市值因子仍不可用（market_cap 空）
