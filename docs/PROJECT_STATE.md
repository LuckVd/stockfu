# stockfu 项目状态（工作日志 / 冷启动手册）

> 新会话先读这份，就能接上。项目：`/opt/pro/stockfu/`。
> 定位：**StockFu·资产管理终端**，借鉴 `../daily_stock_analysis` 的多数据源 fallback 思想，**Web（FastAPI+Vue3）+ CLI**；TUI 已移除。

## 1. 一句话现状
本地优先的综合资产管理 + 市场情绪终端：持仓、股息/网格、三层 fear/greed/heat、历史回补、AI 4 顾问、**天级回测四层架构**（算子→策略→rebalancer→执行；防未来函数）。SQLite + FastAPI + Vue3。

> **【2026-07-21 口径修正】** 此前全周期总表/选股/荐股数字跑在**混复权**行情上，**全部作废**。行情已统一前复权；股息率分母改为 **不复权 `close_raw`**（名义现金 ÷ 全样本 qfq 会虚高并引入前视）。干净全周期结果待 `close_raw` 回补完成 + `--update-backtests` 重跑后重写。

## 0. 进行中任务 / 冷启动接棒（2026-07-21）

### 0.1 正在跑

| 任务 | 状态 |
|------|------|
| 三复权回补 | `python3 main.py --backfill-adj-prices`：**baostock 串行**，默认 Clash SOCKS5 `127.0.0.1:7891`；写 `*_qfq/*_raw/*_hfq`；日志 `data/backfill_adj_prices_baostock_serial.log` |
| 干净全周期回测 | 等 raw 覆盖就绪后：`python3 main.py --update-backtests --start 2021-01-01 --end 2026-07-20` |

### 0.1b 荐股（CLI 已固化）

```bash
python3 main.py --recommend --strategies cross_section_factor,reversal_cross_section \
  --as-of 2026-07-17 --cash 1000000
# 产物 data/reports/recommend/…（runtime，gitignore）
```

### 0.2 已完成（代码层，本轮可提交）

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

### 0.3 全周期结果总表

**待重写。** 勿引用 07-19/07-20 混复权表。干净样本（部分）：

| strategy_id | 备注 |
|-------------|------|
| `reversal_cross_section` | 干净重算约 +5% / 超额 −3%（热缓存全周期 ~3min） |
| `dividend_cross_section` | 旧 +67% **不可信**（qfq 分母）；待 raw 回补后重跑 |

### 0.4 空仓选股

服务可用；**产物需在干净缓存上重跑**，勿用旧 picks 邮件结论。

### 0.5 关键 CLI

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
