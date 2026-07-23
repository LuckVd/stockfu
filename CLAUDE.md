# StockFu · 资产管理终端

本地优先的综合资产 + 市场情绪终端:A股/港股/美股/ETF 持仓管理、TTM 股息率/网格、三层(市场/板块/个股)fear-greed-heat 情绪指数、历史回补、AI 4 顾问、天级回测(算子→策略→选股→执行 四层架构)。技术栈:Python + SQLModel/SQLite + FastAPI + Vue3 前端（看板走 Web；TUI 已移除）。

## 冷启动(新会话先读)
- **`.local/WORKSTATE.md`** — 当前 worktree 的短交接状态（不存在则按 `docs/WORKSTATE_TEMPLATE.md` 创建）；先确认 `pwd` / 分支 / 未提交改动。跨工具通用规则见 `AGENTS.md`。
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
python3 main.py --fetch --date 2026-07-22   # 抓截至该交易日行情+三层情绪（--date 必填；未来/未收盘/非交易日报错，凌晨防误判）
python3 main.py --backfill-dividend
python3 main.py --backfill-adj-prices --start 2020-01-01 --end 2026-07-20
  # baostock 串行三复权;默认 --proxy-mode free（免费代理池+Clash种子，失败自动切换）
  # 默认断点续传(跳过 raw/hfq 已完成的 code);--full 强制全量重抓
python3 main.py --clear-dividend-cache
python3 main.py --backtest bollinger_monthly --start 2025-06-01 --end 2026-01-01 --codes 600519,000858 --save
python3 main.py --update-backtests   # 全周期;可 --strategies a,b
  # 加 BACKTEST_PROGRESS=1 输出 1% 粒度进度日志(每 1% 打印耗时);后台跑用 setsid 脱离会话
  #   setsid bash -c 'BACKTEST_PROGRESS=1 exec python3 -u main.py --update-backtests' > log 2>&1 < /dev/null &
python3 main.py --list-strategies
python3 main.py --recommend --strategies cross_section_factor --as-of 2026-07-17
nohup python3 main.py --schedule >> data/schedule.log 2>&1 &
ruff check stockfu/ main.py tests/        # 代码检查(基线 F 类,当前应全绿;详见 pyproject.toml)
ruff check --fix stockfu/ main.py tests/  # 自动修未用 import/变量等
```

## 关键约定(踩坑点)
- **Python 用 `python3`**;系统 Python PEP668,pip 加 `--break-system-packages`
- **代理**:港美股(yfinance)走 7890(`source /opt/clash/proxy.sh`);国内源(akshare/efinance)no_proxy 直连
- **baostock** 是裸 TCP(不认 HTTP_PROXY);三复权回补默认免费代理池(`--proxy-mode free`，HTTP CONNECT/SOCKS，需 `PySocks`)，失败剔除换 IP;**查询超时也强制换 IP**(`BAOSTOCK_FETCH_TIMEOUT` 默认 60s，防 login 通过却卡在内部接收循环的坏代理);池自愈(耗尽重拉 `BAOSTOCK_REBOOTSTRAP_*`、死IP TTL `BAOSTOCK_DEAD_TTL`、常驻刷新 `BAOSTOCK_MAX_AGE`/`BAOSTOCK_MIN_ALIVE`);**代理池+rebootstrap 耗尽→直连兜底**(`BAOSTOCK_DIRECT_FALLBACK` 默认 on,IP 解封可用;`_MAX`/`_COOLDOWN` 限流,长通道 `maybe_refresh` 池回血后切回);源经 clash 拉、可外置 `data/proxy_sources.json`(`BAOSTOCK_SOURCE_PROXY`)
- **数据**在 `data/stockfu.db`(WAL);回测产物 `data/backtest/`(gitignore)
- **回测防未来函数**:取数 `<= as_of`
- **价格口径**(`quote_snapshot`):
  - 成交/动量/低波等:**前复权** `*_qfq`(遗留 open/high/low/close ≡ qfq)
  - 股息率分母:**不复权** `close_raw`(禁止用 qfq 当分母)
  - 后复权 `*_hfq` 备用
- **量化四层**:算子(math 连续 score)+策略 yaml+rebalancer+engine;算子缓存 fingerprint 含源码 hash
- **行情拆表**:QuoteSnapshot / EtfQuoteDaily / IndexQuoteDaily;`quote_model_for` 路由
- **入库统一收口 + 日期驱动**(`stockfu/services/quote_writer.py`):三张行情表各 1 个 canonical writer(`upsert_quote_snapshot`/`upsert_etf_daily`/`upsert_index_daily`),**严禁别处 `s.add(QuoteSnapshot...)`**;writer 硬保证 `quote_date <= cap_date`(超 cap 的源 bar 一律丢弃)。`--fetch` **必带 `--date YYYY-MM-DD`**,非法(未来/当日未收盘[北京16:00]/非交易日)→ `validate_ingest_date` 报错退出(凌晨不再误判为未开盘的今天);`--schedule` 自动取已收盘最近交易日。stamp 表(资金流/三层情绪/板块资金流)与读窗(composite/fundflow series)统一 `as_of=target_date`
- **官方 K 回补串行** `backfill_kline`;`--fetch` 只刷自选,不全市场;**A 股个股 `--fetch` 走 baostock 三复权**(全字段 + 当日 `close_raw`,baostock 全失败即放弃、**不降级东财/腾讯**;ETF→akshare、港美股→yfinance、指数→manager)
- **代码检查**:`pyproject.toml [tool.ruff]` 已配,基线只启用 `F`(未用 import/变量/未定义名/无占位符 f-string),专防 6219e10 那类「重构后名字作用域错位」的 NameError 回归;默认规则集另有 16 个 E 类遗留(E741 模糊变量名 `l` / E702 / E701 / E402,均非 bug),清理后再 `select=["E","F"]`。改完代码顺手 `ruff check`

## 状态
🚧 MVP。代码:数据层(qfq 硬化)+回测四层+横截面策略族+全周期 CLI+荐股+三复权字段。  
**干净回测 19 产物已完成**（§0.6 全表，07-21 口径，带回撤/卡玛/水下分布）。本会话修了两处性能坑：bollinger 算子 N+1（改预载，提速~20-27x）、value 指纹分裂（params 对齐）——排查见 `docs/PROJECT_STATE.md` §0.8。旧混复权表已作废删除。
