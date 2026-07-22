# stockfu 项目状态（工作日志 / 冷启动手册）

> 新会话先读这份，就能接上。项目：`/opt/pro/stockfu/`。
> 定位：**StockFu·资产管理终端**，借鉴 `../daily_stock_analysis` 的多数据源 fallback 思想，**Web（FastAPI+Vue3）+ CLI**；TUI 已移除。

## 1. 一句话现状
本地优先的综合资产管理 + 市场情绪终端：持仓、股息/网格、三层 fear/greed/heat、历史回补、AI 4 顾问、**天级回测四层架构**（算子→策略→rebalancer→执行；防未来函数）。SQLite + FastAPI + Vue3。

> **【2026-07-21 口径修正】** 此前全周期总表/选股/荐股数字跑在**混复权**行情上，**全部作废**。行情已统一前复权；股息率分母改为 **不复权 `close_raw`**（名义现金 ÷ 全样本 qfq 会虚高并引入前视）。`close_raw` 回补已完成（2026-07-22，100%）；红利策略 `--update-backtests` 重跑中，完成后重写 §0.6。

## 0. 进行中任务 / 冷启动接棒（2026-07-22）

> **干净回测 12/16 已落盘**（§0.6 全表，带回撤/卡玛）：红利 2（07-22 04:03）+ qfq 5（07-21）+ 本次提速重启 5（macd_cross / momentum_breakout×2 / reversal_strategy / cn_momentum_rotation）。
> **【2026-07-22 · 回测性能修复】** ① bollinger 的行情 N+1 已改为日期预载；② value 的多年 PE/PB 分位已改为 5 年内存预载；③ value 参数指纹已对齐，复用其他策略缓存；④ MACD 周线、TTM 分红事件及 `close_raw` 分母均已补入预载，数学算子热路径不再逐 `(code,as_of)` 查询数据库。
> **剩余 4 策略尚未完成**：修复已合入 `main`，现在需以一个持久化、串行的后台任务重启：`pure_factor`/`dual_bollinger`/`bollinger_reversion`/`bollinger_reversion_cross_section`。
> **下次会话**：通过宿主机 PID、日志和产物核验完成状态 → 补满 §0.6 全 16 表。

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

> ✅ 12/16 干净结果已落盘（§0.6）；🔄 **剩余 4 策略由 PID `2820372` 串行运行中**（隔离 shell 不可见，勿据此误启动第二个任务）。

1. **通过宿主机检查串行批处理状态**：  
   ```bash
   ps -p $(cat data/update_backtests_rest.pid) -o etime= 2>/dev/null || echo DONE
   tail -20 data/update_backtests_rest.log
   ```
2. **补满 §0.6 全表**：完成后从 `upd-*-2026-07-20.json.gz` 取这 4 个数字填入 §0.6。
3. 可选：`--recommend` 在干净缓存上重跑选股。

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

### 0.6 全周期结果总表（2026-07-22 干净重跑，12/16）

> 区间 2021-01-01 → 2026-07-20，universe=all（815 票），基准上证 +8.37%。按总收益降序。**勿引用 07-19/07-20 混复权 / qfq 分母旧表。**

| 策略 | 总收益 | 年化 | 回撤 | 夏普 | 卡玛 | 超额 | 换手/年 | 读法 |
|------|------:|-----:|-----:|-----:|-----:|-----:|-------:|------|
| `momentum_breakout` | **+106.3%** | 14.6% | 33.6% | 0.58 | 0.43 | +97.9% | 6.8 | 动量追涨；2025 一年 +70% 撑起，卫星 |
| `momentum_breakout_cross_section` | +90.2% | 12.8% | 38.5% | 0.58 | 0.33 | +81.8% | 8.8 | 同上横截面版，回撤更大 |
| **`dividend_cross_section`** | +50.6% | 8.0% | **14.2%** | **0.63** | **0.56** | +42.2% | **1.2** | ✅ 核心候选；回撤最低/卡玛最高/换手极低 |
| `cn_momentum_cross_section` | +27.9% | 4.7% | 43.4% | 0.31 | 0.11 | +19.5% | 7.1 | 回撤大、卡玛低 |
| `etf_momentum_cross_section` | +20.9% | 3.6% | 19.1% | 0.37 | 0.19 | +12.6% | 11.2 | 回撤尚可但收益薄 |
| `etf_momentum_rotation` | +15.0% | 2.6% | 21.6% | 0.31 | 0.12 | +6.6% | 10.9 | 平 |
| `cn_momentum_rotation` | +12.4% | 2.2% | 45.6% | 0.21 | 0.05 | +4.0% | 7.1 | ❌ 回撤 45% 全场最高 |
| `reversal_strategy` | +8.6% | 1.6% | 36.7% | 0.18 | 0.04 | +0.2% | 8.7 | 贴基准、回撤大 |
| `cross_section_factor` | +6.3% | 1.1% | 37.8% | 0.15 | 0.03 | −2.1% | 6.4 | 同上 |
| `reversal_cross_section` | +5.0% | 0.9% | 37.6% | 0.15 | 0.02 | −3.3% | 6.9 | 同上 |
| `dividend_low_vol` | −8.9% | −1.7% | 34.4% | −0.10 | −0.05 | −17.2% | 9.6 | ❌ 同因子 top_n 锁仓版跑输 |
| `macd_cross` | −14.4% | −2.9% | 38.0% | −0.22 | −0.08 | −22.8% | 9.2 | ❌ 证伪 |

> **风险调整看卡玛**：`dividend_cross_section`（0.56）远胜动量双雄（0.33–0.43）——收益高且回撤仅 14%，适合核心仓位；动量双雄收益高但回撤 34–38%、+70% 集中在 2025 顺风年，宜作卫星。`momentum_breakout` 分年：2022 −14% / 2024 +10% / **2025 +70%** / 2026 +15%。
>
> 🔄 **剩余 4 个串行运行中**（§0）：`pure_factor` / `dual_bollinger` / `bollinger_reversion` / `bollinger_reversion_cross_section`，完成后补入本表凑齐 16。

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

1. **算子 N+1 查库**：算子 `run()` 内 `with session_scope() as s: select(...)`（违反"算子不直接取库"契约）→ 每个 (code,as_of) 一次 DB 查询，全周期 ~百万次。**修法**：改走 `quote_series` / `quote_series_dates` 预载（回测内存供给器，零 DB）。已修 `monthly/weekly_bollinger`；自查 `grep session_scope stockfu/ai/operators/factors/*.py`。
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
