# stockfu 项目状态（工作日志 / 冷启动手册）

> 新会话先读这份，就能接上。项目：`/opt/pro/stockfu/`。
> 定位：**StockFu·资产管理终端**，借鉴 `../daily_stock_analysis` 的多数据源 fallback 思想，**Web（FastAPI+Vue3）+ CLI**；TUI 已移除。

## 1. 一句话现状
本地优先的综合资产管理 + 市场情绪终端：持仓、股息/网格、三层 fear/greed/heat、历史回补、AI 4 顾问、**天级回测四层架构**（算子→策略→rebalancer→执行；防未来函数）。SQLite + FastAPI + Vue3。

> **【2026-07-21 口径修正】** 此前全周期总表/选股/荐股数字跑在**混复权**行情上，**全部作废**。行情已统一前复权；股息率分母改为 **不复权 `close_raw`**（名义现金 ÷ 全样本 qfq 会虚高并引入前视）。`close_raw` 回补已完成（2026-07-22，100%）；红利策略 `--update-backtests` 已重跑完成（2026-07-23），§0.6 已重写为 07-21 口径全表（19 产物）。

## 当前重点:两大方向与共同底层（2026-07-23）

项目当前聚焦两个方向,共用同一套底层数据基础设施:单库 `data/stockfu.db` + 统一 fetch 链。代码现状:`main` 已与 `origin/main` 同步、工作树干净。

### 方向一 · 回测（off-line 量化研究）

- **四层架构**:算子(math 连续 score)→ 策略 yaml → rebalancer → engine;严格防未来函数(取数 `<= as_of`);算子缓存(fingerprint 含源码 hash,改算子须 +1 version 或删缓存)。
- **入口**:`--backtest <id>` 单策略、`--update-backtests` 全周期批跑、`--recommend` 荐股、`--list-strategies`、`--factor-diag <op>`。
- **现状**:19 份干净结果已落盘(§0.6 全表;2021-01-04→2026-07-21,前复权 + 股息率分母 `close_raw`,基准上证 +10.32%);详见 `docs/BACKTEST.md`。

### 方向二 · 实时生图（当日行情 → 可视化 → 推送）

- **数据流**:`--fetch --date YYYY-MM-DD` 抓截至该交易日 → 落库天级快照(统一收口 `quote_writer`,盖章=目标日) → 三层情绪(市场/板块/个股 fear·greed·heat,读窗 `as_of=目标日`)→ Web 看板与 9:16 分享卡片实时渲染 → playwright 无头截图 → SMTP 邮件。`--date` 必填,非法(未来/未收盘/非交易日)报错;`--schedule` 自动取已收盘最近交易日。
- **入口**:`--serve`(Web 看板 127.0.0.1:8787)、`--test-mail`(立即生图 + 发测试邮件)、`--schedule`(APScheduler 每日 cron:fetch → mail)。
- **实现**:`stockfu/services/mail.py`(`render_share_images` playwright 逐页截图 + `send_card_email` SMTP inline 多图);依赖 `playwright install chromium`,且生图时需 `--serve` 在跑。
- **⚠️ 出图避坑**:主页 `loadAll` 会并发实时算估值、拉起 baostock 代理池 spin 卡死;生图只取 `/share`(纯读库、不等 loadAll),建议配 `BAOSTOCK_PROXY_MODE=direct`。

### 共同底层(两方向都依赖)

- **DB**:单一 `data/stockfu.db`(WAL;SQLModel/SQLite)。路径由 `stockfu/config.py` 的 `BASE_DIR = Path(__file__).resolve().parent.parent` → `DATA_DIR = BASE_DIR/"data"` 解析;可用 `.env` 的 `DB_URL` 覆盖(当前未用)。回测读历史、生图读当日,**共用同一张 `quote_snapshot`**(三复权 `*_qfq`/`*_raw`/`*_hfq`,成交/动量用 qfq、股息率分母用 raw)+ index/etf 拆表。
- **fetch**:统一抓取链 `--fetch` / `run_scheduled_fetch`(`stockfu/scheduler/jobs.py`),按品种路由——A 股个股 → baostock 三复权(全字段 + 当日 `close_raw`,失败不降级,默认免费代理池 `--proxy-mode free`);ETF → akshare;港美股 → yfinance(本机 clash `7890`);指数 → akshare(`no_proxy` 直连)。**只刷自选**,落库后算三层情绪。历史回补另走 `--backfill-adj-prices`(baostock 串行三复权,断点续传)。

## 0. 进行中任务 / 冷启动接棒（2026-07-22）

> 🔥 **【2026-07-24 · 聚宽交叉验证 · 当前主线】** 用聚宽 **jqboson 独立回测引擎**重跑策略3 `dividend_cross_section#sl30`,验证本地结论(年化 10.65%/回撤 16.2%/夏普 0.78/年换手 0.61)在另一套数据+撮合引擎下成立。分支 `feat/joinquant-crosscheck`(已推送 `f6a371d`);脚本 `scripts/joinquant_dividend_sl30.py`(**本会话改动未提交 git**)。
> **进度:三因子全部跑通 + 诊断全删 + 口径逐行对齐本地**(核对 `stockfu/services` + `ai/operators/factors` 源码)。2021-01-04~06 探针验证:601288 红利20/低波17.7/价值5.2、601098 红利20/低波17.3/价值20,**选股转银行红利价值(农行/中信/工行),与本地 sl30 风格一致**。
> **jqboson 四大坑(全踩平,详见脚本注释 + memory `joinquant-crosscheck-env`)**:① `get_price` 多只股票返回**长格式** DataFrame(`['time','code','close']`,非横截面/非官方 Panel 式)→ `_normalize_panel` 探测 code/security 标识列 + time 排序归一;② `dir(finance)` 对 `__getattr__` 动态表返回空 → `_resolve_dividend_source` 用 **hasattr** 逐候选枚举;③ 无 `finance.STK_DIVIDEND` 表;④ `STK_XR_XD` 无每股现金字段,用 **`bonus_ratio_rmb`(每10股派息RMB)÷10÷不复权价** + **`a_xr_date`(除权除息日)**(≡ 本地 `dividend_yield_ttm`)。
> **口径对齐**:红利 bonus10 ≡ 本地;低波 bar 数 `3*365+20+30=1145`、分位 mid-rank `(below+equal/2)/n`(对齐 `factors.percentile`/`valuation._percentile_sorted`)。**唯一不可避差异**:价值因子本地用日频 PE(~1250点),jqboson `get_fundamentals` 只能逐日查、日频不可行(160万次)→ 脚本月采样(61点),分位排序影响很小、阈值一致,已注明勿改。**不缩窗口/不降采样**(用户明确不偏离口径)→ 全周期(5年/1300交易日)聚宽免费版必超时,接受或分段跑。
> **接棒**:① 跑验证曲线(建议先 6 个月 2021-01-04~07-01)对照本地量级;② 全周期分段跑拼曲线;③ 验证 OK 后提交 git + 开 PR(剩可接受差异:universe 固定788 vs 本地动态池、竞争键、止损 T+1、科创板688市价单需限价)。详见 `.local/WORKSTATE.md`。

> **【2026-07-24 · 邮件生图数据正确性修复】** 三件事，均在分支 `fix/dividend-ttm-corruption`：
> ① **股息率虚高**（commit `ca056b9`）：baostock 抓分红结果集串行 bleed 产生 stale 行（2017 标签配 2026 ex_date/28 元）+ TTM 计算只有下界无上界（future ex_date 计入）+ 持仓/自选股息率分母误用 qfq `close`。修：`dividend.metric_from_db`/`baostock.get_dividend_metric` TTM 加上界 `≤today`；baostock 解析丢弃 `|ex_date年−财年|≥2` 的 stale 行；`persist_dividends` 同批去重；`snapshot.LatestSnapshot` 加 `close_raw`、`portfolio` 两处分母改 `close_raw`。五粮液 48%→6.89%。本机 `dividend_event` 清 44 行（5 stale+38 重复+1 phantom）。
> ② **ETF 显示陈旧数据**：`snapshot._read_latest` 与 `share.perf` 硬编码读 `QuoteSnapshot`、没按 `quote_model_for` 路由 → 7 只自选 ETF 读到 `QuoteSnapshot` 里停在 07-21 的孤儿行（ETF 行情实际在 `EtfQuoteDaily`），卡片显示旧价（588870 曾显示 07-21 的 1.941/+10.85%）。已改这两处路由，全 52 holdings 现 07-23（588870 科创50ETF 1.819/-3.24%）。**⚠️ 仅修了卡片两条路径；其余 reader 未修见 §8.6。**
> ③ **港美股清除**（保留 yfinance 代码能力）：DB 删 4 Asset+2 Holding；`config.py` watchlist + `db.py` demo holdings 去掉 `00700/09988/AAPL/MSFT`。剩 52 个纯 A 股资产。
> 已 `--fetch --date 2026-07-23`（行情至 07-23）+ `--test-mail` 重发正确邮件（`BAOSTOCK_PROXY_MODE=direct`）。**PR 待开**（`gh` 未装，手动点链接）；committer 仍 `root@localhost`。

> ✅ **【已完成 2026-07-22】策略参数变体（一等）+ 回测指标持久化** —— 按 `docs/STRATEGY_VARIANTS_PLAN.md` 全量实现并提交（main `561a0e6`+`7a87328`；backtest `feature/backtest` `e96f598`+`4c8a295`）。A) `strategy_id` 编码变体（`base#key`，seed `_expand_variants`/`_deep_merge` 展开器；变体行 derived 每次 seed 强制重同步；recommend 改读 DB config；main 校验复合 id 不静默回落）；B) 引擎原生产出 回本/本金水下分布（低于初始本金、以及亏损至少 10/20/30%）/distinct_bought/stop_loss_count/realized_loss（止损 signal 穿透 6 处，原 `_exec` 写 `None` 丢失）+ schema 1→2。单测 72/72；e2e：base 8% stop_loss=11 vs `#sl30` 30% stop_loss=1（B3 穿透实证）。首个用例 `dividend_cross_section`(8%) + `dividend_cross_section#sl30`(30%) 并存。main DB 已 reseed base 8%（修历史 30% 污染）+ 新增 sl30。附带修复 `asset.note` 遗留列（`_migrate` DROP，干净库 `--init-db` 跑通）。
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

> ✅ **16/16 干净结果已落盘**（§0.6 全表，07-22 18:11 完成，`ok:4 fail:0`）。PID `2880236` 已退出，无残留进程。（07-23 新增 `#sl30w10`/`#sl30w20` 变体，现共 19 产物，详见 §0.6。）

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

### 0.6 全周期结果总表（19 策略/变体，2026-07-21 口径）

> 权威产物为 `data/backtest/upd-*-2026-07-21.{json.gz,meta.json}`（19 份，schema-2，直接读 gz）。初始 100 万，2021-01-04→2026-07-21，前复权，股息率分母 `close_raw`，基准上证 +10.32%。`止损损(万)` 为负值＝已实现止损亏损；`水下天%`＝净值低于初始本金的交易日占比（本金水下口径，非相对历史峰值）；`回本✗`＝至期末净值仍未回到最大回撤前高。

| 策略(中文) | id | 止损 | 单股 | 年化% | 总收% | 回撤% | 卡玛 | 夏普 | 索提诺 | 超额% | 水下天% | 亏≥10% | 亏≥20% | 亏≥30% | 回本天 | 回本 | 胜率% | 交易 | 止损次 | 止损损(万) | 年换手 | 买过 | 均仓% | 单股峰% | 手续费(万) | 终值(万) |
|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|:--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| 月线动量突破 | `momentum_breakout` | 8 | 10 | 14.6 | 106.3 | 33.6 | 0.4 | 0.6 | 0.6 | 96.0 | 43.3 | 6.7 | 0.0 | 0.0 | 174 | ✓ | 41.6 | 5118 | 329 | -203.6 | 6.8 | 492 | 70.9 | 29.6 | 15.0 | 206.3 |
| 动量突破横截面 | `momentum_breakout_cross_section` | 8 | 5 | 12.8 | 90.2 | 38.5 | 0.3 | 0.6 | 0.6 | 79.8 | 5.6 | 0.0 | 0.0 | 0.0 | 318 | ✓ | 39.4 | 9232 | 766 | -232.5 | 8.8 | 713 | 72.4 | 13.0 | 14.0 | 190.1 |
| 红利横截面(止损30%) | `dividend_cross_section#sl30` | 30 | 5 | 10.7 | 71.5 | 16.2 | 0.7 | 0.8 | 0.8 | 61.1 | 0.6 | 0.0 | 0.0 | 0.0 | 9 | ✓ | 53.3 | 895 | 6 | -8.4 | 0.6 | 157 | 93.6 | 8.2 | 1.7 | 171.5 |
| 红利横截面(止损30%,单股10%) | `dividend_cross_section#sl30w10` | 30 | 10 | 8.7 | 55.6 | 16.8 | 0.5 | 0.6 | 0.7 | 45.3 | 0.5 | 0.0 | 0.0 | 0.0 | 9 | ✓ | 50.5 | 1162 | 6 | -11.5 | 0.8 | 142 | 89.8 | 15.7 | 2.9 | 155.6 |
| 红利横截面(高股息+低波+价值) | `dividend_cross_section` | 8 | 5 | 8.0 | 50.6 | 14.2 | 0.6 | 0.6 | 0.6 | 40.2 | 0.9 | 0.0 | 0.0 | 0.0 | 356 | ✓ | 47.4 | 1655 | 170 | -60.5 | 1.2 | 196 | 92.0 | 7.9 | 3.1 | 150.6 |
| 个股动量横截面 | `cn_momentum_cross_section` | 8 | 5 | 7.1 | 44.2 | 43.0 | 0.2 | 0.4 | 0.4 | 33.9 | 69.5 | 43.5 | 3.9 | 0.0 | 247 | ✓ | 37.2 | 7268 | 646 | -145.0 | 6.3 | 729 | 79.9 | 19.1 | 9.4 | 144.2 |
| 双布林带(周+月) | `dual_bollinger` | 8 | 10 | 4.5 | 26.2 | 42.6 | 0.1 | 0.3 | 0.3 | 15.9 | 59.3 | 33.7 | 3.9 | 0.0 | 416 | ✓ | 42.6 | 6391 | 407 | -212.0 | 5.7 | 566 | 93.4 | 20.1 | 17.5 | 126.2 |
| ETF动量横截面 † | `etf_momentum_cross_section` | 8 | 5 | 3.6 | 20.6 | 19.1 | 0.2 | 0.4 | 0.4 | 10.3 | 25.9 | 0.0 | 0.0 | 0.0 | 249 | ✓ | 40.9 | 2766 | 10 | -3.7 | 11.2 | 27 | 39.1 | 8.0 | 5.0 | 120.6 |
| 红利横截面(止损30%,单股20%) | `dividend_cross_section#sl30w20` | 30 | 20 | 3.2 | 18.2 | 26.2 | 0.1 | 0.3 | 0.3 | 7.9 | 77.9 | 46.3 | 2.8 | 0.0 | 205 | ✓ | 48.5 | 1537 | 16 | -10.9 | 0.8 | 171 | 83.0 | 28.4 | 3.7 | 118.2 |
| ETF动量轮动 † | `etf_momentum_rotation` | 8 | 18 | 2.9 | 16.6 | 21.6 | 0.1 | 0.3 | 0.3 | 6.3 | 81.2 | 35.3 | 0.0 | 0.0 | 691 | ✓ | 41.2 | 888 | 7 | -7.0 | 10.9 | 27 | 24.0 | 23.6 | 3.6 | 116.6 |
| 降换手动量轮动 | `cn_momentum_rotation` | 8 | 12 | 2.2 | 12.4 | 45.6 | 0.1 | 0.2 | 0.2 | 2.1 | 90.4 | 70.1 | 61.4 | 41.2 | 319 | ✓ | 38.7 | 5004 | 284 | -79.9 | 7.1 | 573 | 67.3 | 15.0 | 8.6 | 112.4 |
| 反转均值回归 | `reversal_strategy` | 8 | 12 | 1.8 | 9.9 | 36.7 | 0.1 | 0.2 | 0.2 | -0.4 | 83.3 | 75.0 | 44.4 | 5.4 | 231 | ✓ | 51.6 | 5257 | 466 | -166.5 | 8.7 | 505 | 66.7 | 17.2 | 10.0 | 109.9 |
| 横截面多因子 | `cross_section_factor` | 8 | 5 | 1.1 | 6.3 | 37.8 | 0.0 | 0.1 | 0.2 | -4.0 | 76.2 | 54.9 | 20.0 | 1.4 | 344 | ✓ | 49.6 | 7729 | 1189 | -223.2 | 6.4 | 632 | 82.5 | 16.0 | 9.7 | 106.3 |
| 反转横截面 | `reversal_cross_section` | 8 | 5 | 0.9 | 5.0 | 37.6 | 0.0 | 0.1 | 0.1 | -5.3 | 67.1 | 50.7 | 20.2 | 0.7 | 244 | ✓ | 52.9 | 8295 | 1179 | -251.6 | 6.9 | 680 | 84.4 | 16.9 | 11.2 | 105.0 |
| 纯因子动量反转 | `pure_factor` | 8 | 10 | 0.9 | 4.9 | 55.2 | 0.0 | 0.2 | 0.2 | -5.4 | 73.3 | 64.9 | 45.9 | 6.0 | – | ✗ | 42.3 | 6230 | 403 | -134.0 | 6.0 | 501 | 71.7 | 16.0 | 11.8 | 104.9 |
| 布林回归横截面 | `bollinger_reversion_cross_section` | 8 | 5 | -1.2 | -6.1 | 38.5 | -0.0 | 0.0 | 0.0 | -16.4 | 84.4 | 53.2 | 14.4 | 0.6 | 465 | ✓ | 50.0 | 9869 | 1049 | -177.5 | 6.5 | 772 | 89.8 | 17.0 | 12.3 | 93.9 |
| 红利低波 | `dividend_low_vol` | 8 | 12 | -1.7 | -8.9 | 34.4 | -0.1 | -0.1 | -0.1 | -19.2 | 79.7 | 52.0 | 3.9 | 0.0 | – | ✗ | 49.8 | 4839 | 200 | -69.5 | 9.6 | 254 | 67.2 | 16.2 | 10.7 | 91.1 |
| 布林均值回归 | `bollinger_reversion` | 8 | 10 | -2.5 | -12.9 | 39.7 | -0.1 | -0.1 | -0.1 | -23.2 | 99.3 | 81.2 | 40.7 | 5.4 | – | ✗ | 51.4 | 7451 | 327 | -94.2 | 7.4 | 610 | 79.1 | 20.2 | 17.5 | 87.1 |
| MACD金叉死叉 | `macd_cross` | 8 | 10 | -2.9 | -14.4 | 38.0 | -0.1 | -0.2 | -0.2 | -24.7 | 57.0 | 27.8 | 0.0 | 0.0 | – | ✗ | 41.7 | 6887 | 41 | -7.3 | 9.2 | 220 | 44.2 | 15.0 | 14.5 | 85.6 |

> † ETF 族 universe n=27，基准仍取上证窗口，超额仅供参考、不与 A 股 n=815 横比。
>
> **红利 4 变体（止损线 × 单股权重）**：甜点是 `#sl30`（30% 止损 + 单股 5%，年化 10.7 / 卡玛 0.7 / 夏普 0.8 / 水下天 0.6% / 回本 9 天 / 止损仅 6 次损 8.4 万）。base 8% 紧止损砍 170 次损 60.5 万、回本拖到 356 天。加单股上限是反方向：`#sl30w10`(≤10%) 卡玛降到 0.5；`#sl30w20`(≤20%) 单股峰 28.4%、水下天飙到 77.9%、回撤 26.2%。红利靠分散，集中单股无好处。
>
> **风险调整看卡玛/夏普**：`#sl30`（0.7/0.8，全场最高）> base（0.6/0.6）> 动量双雄（0.3–0.4 / 0.6）。动量双雄收益最高（年化 12.8–14.6%）但回撤 34–38%、水下天 5.6–43.3%，宜作卫星。`momentum_breakout` 分年：2022 −14% / 2024 +10% / **2025 +70%** / 2026 +15%。
>
> **水下分布**：全程在水面上的只有红利系（base/#sl30/#sl30w10 水下天 <1%）——这是红利相对全场的护城河。反面：`bollinger_reversion` 99.3%、`cn_momentum_rotation` 90.4%（亏≥20% 天 61.4%）、`etf_rotation` 81.2%。
>
> **止损视角**：动量类止损损失最重（横截面 232.5 万 / 月线 203.6 万）但靠趋势仍最赚——止损是「截断亏损让利润奔跑」的成本；反转横截面(251.6 万)/横截面多因子(223.2 万)止损损失最大且策略本身不赚钱，止损无效纯磨损。**胜率高 ≠ 赚钱**：反转类胜率 50%+ 却跑输（小赚多次大亏一次），动量类 37–42% 反而赚大钱。
>
> ❌ **证伪**（跑输基准且未回本/夏普 ≤0）：`macd_cross` / `bollinger_reversion` / `bollinger_reversion_cross_section` / `dividend_low_vol` / `pure_factor`（纯因子回撤 55% 全场最高且未回本）。
>
> 🆕 **指标集（schema-2）**：回本/回本状态、本金水下分布（低于本金、亏损≥10/20/30% 天数占比）、去重买入数、止损成交/止损损失、卡玛/索提诺。
>
> ✅ **19 份 schema-2 产物已完成**（2026-07-23，07-21 口径）；本金水下指标按原始权益曲线回填。

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
- 代码检查基线（ruff，`pyproject.toml` 启用 F 类，防 NameError 作用域回归）

## 8. 待办

1. ✅ `close_raw` 覆盖完成 + **12/16 干净结果**（§0.6）。剩 4 策略串行运行中，完成后补满全表。
2. ✅ `--fetch` A 股走 baostock 三复权,当日 `close_raw` 自动刷新(无需单独刷)
3. ✅ 回测性能修复（bollinger N+1 + value 指纹，§0.8）
4. 小市值因子仍不可用（market_cap 空）
5. ruff E 类清理（16 个：E741 模糊变量名 `l` / E702 / E701 / E402，均非 bug）后，基线升级 `select=["E","F"]`
6. ⚠️ **【未修·2026-07-24 记录】行情拆表后约 10 处代码仍直接 `select(QuoteSnapshot)` 读 ETF/指数**（未按 `quote_model_for` 路由）：`services/{universe,composite,backfill,fundflow,valuation,indices}.py`、`ai/context.py`、`api/routes.py`、`scheduler/jobs.py`（多处）。ETF 行情已迁到 `EtfQuoteDaily`，但 `QuoteSnapshot` 里残留 ETF 孤儿行（停在 2026-07-21、永不再更新）→ 这些 reader 对 ETF 读到陈旧数据。**影响**：Web 看板 ETF 行情行、ETF 三层情绪（`composite` 读 `QuoteSnapshot` 算）等可能显示旧值；**不影响**邮件分享卡片（`snapshot.latest_snapshot`+`share.perf` 已 2026-07-24 改路由）。**修法**：(a) 给这 ~10 处统一改 `quote_model_for` 路由；(b) 删 ETF/Index 的 `QuoteSnapshot` 孤儿行——但会令部分 reader 读空，须逐个确认（`composite` 算 ETF 情绪 / `fundflow` / `indices`）。**当前决定：暂不修**（邮件已正确），记录在此待后续。
