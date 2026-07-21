# StockFu · 资产管理终端

本地优先的综合资产 + 市场情绪终端:A股/港股/美股/ETF 持仓管理、TTM 股息率/网格、三层(市场/板块/个股)fear-greed-heat 情绪指数、历史回补、AI 4 顾问、天级回测(算子→策略→选股→执行 四层架构)。技术栈:Python + SQLModel/SQLite + FastAPI + Vue3 前端（看板走 Web；TUI 已移除）。

## 冷启动(新会话先读)
- **`docs/PROJECT_STATE.md`** — 工作日志/冷启动手册。**先读 §0**（进行中任务），再读 §1。
- `docs/ROADMAP.md` — 项目路线图
- `docs/BACKTEST.md` — 回测引擎(四层架构 + scheduler + 算子缓存 + metrics)
- `docs/AI_ADVISORS.md` — 实盘 AI 4 顾问

## 运行命令
```bash
cd /opt/pro/stockfu
python3 main.py --init-db          # 建库 + 种子
python3 main.py                    # Web 看板(默认 127.0.0.1:8787)
python3 main.py --serve
python3 main.py --fetch            # 每日抓取 + 三层情绪
python3 main.py --backfill-dividend
python3 main.py --backfill-adj-prices --start 2020-01-01 --end 2026-07-20
  # baostock 串行三复权;默认 --proxy-mode free（免费代理池+Clash种子，失败自动切换）
python3 main.py --clear-dividend-cache
python3 main.py --backtest bollinger_monthly --start 2025-06-01 --end 2026-01-01 --codes 600519,000858 --save
python3 main.py --update-backtests   # 全周期;可 --strategies a,b
python3 main.py --list-strategies
python3 main.py --recommend --strategies cross_section_factor --as-of 2026-07-17
nohup python3 main.py --schedule >> data/schedule.log 2>&1 &
```

## 关键约定(踩坑点)
- **Python 用 `python3`**;系统 Python PEP668,pip 加 `--break-system-packages`
- **代理**:港美股(yfinance)走 7890(`source /opt/clash/proxy.sh`);国内源(akshare/efinance)no_proxy 直连
- **baostock** 是裸 TCP(不认 HTTP_PROXY);三复权回补默认免费代理池(`--proxy-mode free`，HTTP CONNECT/SOCKS，需 `PySocks`)，失败剔除换 IP;池自愈(耗尽重拉 `BAOSTOCK_REBOOTSTRAP_*`、死IP TTL `BAOSTOCK_DEAD_TTL`、常驻刷新 `BAOSTOCK_MAX_AGE`/`BAOSTOCK_MIN_ALIVE`);源经 clash 拉、可外置 `data/proxy_sources.json`(`BAOSTOCK_SOURCE_PROXY`)
- **数据**在 `data/stockfu.db`(WAL);回测产物 `data/backtest/`(gitignore)
- **回测防未来函数**:取数 `<= as_of`
- **价格口径**(`quote_snapshot`):
  - 成交/动量/低波等:**前复权** `*_qfq`(遗留 open/high/low/close ≡ qfq)
  - 股息率分母:**不复权** `close_raw`(禁止用 qfq 当分母)
  - 后复权 `*_hfq` 备用
- **量化四层**:算子(math 连续 score)+策略 yaml+rebalancer+engine;算子缓存 fingerprint 含源码 hash
- **行情拆表**:QuoteSnapshot / EtfQuoteDaily / IndexQuoteDaily;`quote_model_for` 路由
- **官方 K 回补串行** `backfill_kline`;`--fetch` 只刷自选,不全市场

## 状态
🚧 MVP。代码:数据层(qfq 硬化)+回测四层+横截面策略族+全周期 CLI+荐股+三复权字段。  
**干净全周期数字待 raw 回补与重跑后写入 PROJECT_STATE §0.3**（旧混复权表已作废删除）。
