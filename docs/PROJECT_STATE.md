# stockfu 项目状态（工作日志 / 冷启动手册）

> 新会话先读这份，就能接上。项目：`/opt/pro/stockfu/`。
> 定位：**StockFu·资产管理终端**，借鉴 `../daily_stock_analysis` 的多数据源 fallback 思想，**Web（FastAPI+Vue3）+ CLI**；TUI 已移除。

## 1. 一句话现状
本地优先的综合资产管理 + 市场情绪终端：持仓、股息/网格、三层 fear/greed/heat、历史回补、AI 4 顾问、**天级回测四层架构**（算子→策略→rebalancer→执行；防未来函数）。SQLite + FastAPI + Vue3。

> **【2026-07-21 口径修正】** 此前全周期总表/选股/荐股数字跑在**混复权**行情上，**全部作废**。行情已统一前复权；股息率分母改为 **不复权 `close_raw`**（名义现金 ÷ 全样本 qfq 会虚高并引入前视）。干净全周期结果待 `close_raw` 回补完成 + `--update-backtests` 重跑后重写。

## 0. 进行中任务 / 冷启动接棒（2026-07-21 晚）

> **关会话不影响回补**：进程已 **double-fork / PPID=1** 后台跑。

### 0.1 三复权 baostock 串行回补（免费代理池保障）

| 项 | 值 |
|----|-----|
| 命令 | `python3 main.py --backfill-adj-prices --start 2020-01-01 --end 2026-07-20` |
| 代理 | **默认 `--proxy-mode free`**：启动拉公网免费代理入池 + 本机 Clash `7891` 种子；单 IP 串行；失败/黑名单立即剔除并切换 |
| 其它模式 | `--proxy-mode clash` 仅本机 SOCKS；`--proxy-mode direct` / `--no-socks` 直连 |
| 源 | **仅 baostock**；`preserve_qfq=True`（只补 raw/hfq） |
| 实现 | `stockfu/data/free_proxy_pool.py` + `baostock_proxy.py`；HTTP CONNECT / SOCKS 隧道 |
| 日志建议 | `nohup python3 -u main.py --backfill-adj-prices --start 2020-01-01 --end 2026-07-20 > data/backfill_adj_prices_baostock_serial.log 2>&1 &` |
| 覆盖率 | `python3 -c "from stockfu.scheduler.backfill_adj_prices import adj_price_coverage; print(adj_price_coverage())"` |
| 完成标志 | 日志出现 `=== 完成 ok=…`；`raw_pct`/`hfq_pct` 接近 100% |

> 此前直连 IP 已被 baostock 黑名单；勿用 `--proxy-mode direct`。进程可 double-fork 后台跑。

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
so=open('data/backfill_adj_prices_baostock_serial.log','ab')
os.dup2(open('/dev/null','rb').fileno(),0); os.dup2(so.fileno(),1); os.dup2(so.fileno(),2)
os.execvp('python3',['python3','-u','main.py','--backfill-adj-prices','--start','2020-01-01','--end','2026-07-20'])
PY
```

### 0.2 下一步（回补完成后做）

1. **确认覆盖**  
   `adj_price_coverage()` → `has_raw` 与全表行数接近；抽检  
   `601919@2023-06-30`：`close_raw≈9.4`，`close_qfq` 仍为前复权。
2. **清股息缓存（回补 CLI 结束时会自动清；若中途重跑可手动）**  
   `python3 main.py --clear-dividend-cache`
3. **干净全周期回测**（优先红利两只，再全目录）  
   ```bash
   nohup python3 -u main.py --update-backtests --start 2021-01-01 --end 2026-07-20 \
     --strategies dividend_cross_section,dividend_low_vol \
     > data/update_backtests_dividend_raw.log 2>&1 &
   # 或全部:
   nohup python3 -u main.py --update-backtests --start 2021-01-01 --end 2026-07-20 \
     > data/update_backtests_clean.log 2>&1 &
   ```
4. **重写 §0.3 总表**（旧混复权 / qfq 分母红利数字一律作废；含曾见的 +67% 红利 CS）
5. 可选：`--recommend` 在干净缓存上重跑选股

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

### 0.6 全周期结果总表

**待重写。** 勿引用 07-19/07-20 混复权表。干净样本（部分）：

| strategy_id | 备注 |
|-------------|------|
| `reversal_cross_section` | 干净重算约 +5% / 超额 −3%（热缓存全周期 ~3min） |
| `dividend_cross_section` | 旧 +67% **不可信**（qfq 分母）；待 raw 回补后重跑 |

### 0.7 关键 CLI

```bash
python3 main.py --init-db
python3 main.py --serve
python3 main.py --backfill-adj-prices --start 2020-01-01 --end 2026-07-20   # baostock 串行+SOCKS
python3 main.py --clear-dividend-cache
python3 main.py --update-backtests --start 2021-01-01
python3 main.py --list-strategies
python3 main.py --recommend --strategies cross_section_factor --as-of 2026-07-17
```

**踩坑**：① 并行回测易 `database is locked`；② baostock 裸 TCP 不认 HTTP_PROXY，封禁时走 SOCKS；③ 官方行情 backfill **串行**；④ `--fetch` 只刷自选。

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

1. 等 `close_raw` 全市场覆盖 → 清 `dividend_yield` 缓存（回补 CLI 已带）→ 重跑红利/全策略 → 重写 §0.3
2. 可选：日常 `--fetch` 顺带刷当日 close_raw
3. 小市值因子仍不可用（market_cap 空）
