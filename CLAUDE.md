# StockFu · 资产管理终端

本地优先的综合资产 + 市场情绪终端:A股/港股/美股/ETF 持仓管理、TTM 股息率/网格、三层(市场/板块/个股)fear-greed-heat 情绪指数、历史回补、AI 4 顾问、天级回测(算子→策略→选股→执行 四层架构)。技术栈:Python + SQLModel/SQLite + FastAPI + Vue3 前端（看板走 Web；TUI 已移除）。

## 冷启动(新会话先读)
- **`.local/WORKSTATE.md`** — 当前任务的短交接状态（不存在则按 `docs/WORKSTATE_TEMPLATE.md` 创建）；先确认 `pwd` / 分支 / 未提交改动。Git 分支规矩与跨工具通用规则见 `AGENTS.md`。
- **`docs/BACKTEST.md`** — 回测系统唯一权威文档：当前基线、准确性缺口、目标架构、实施阶段与验收门禁。
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
nohup python3 main.py --schedule >> data/schedule.log 2>&1 &   # 常驻:工作日到点 fetch→算指数→出图→发信(内嵌 web 单进程)
python3 main.py --test-mail            # 手动出图+发信(需 --serve 在跑;出图必加 BAOSTOCK_PROXY_MODE=direct 防 loadAll 挂死)
ruff check stockfu/ main.py tests/        # 代码检查(基线 F 类,当前应全绿;详见 pyproject.toml)
ruff check --fix stockfu/ main.py tests/  # 自动修未用 import/变量等
```

## 关键约定(踩坑点)
- **Python 用 `python3`**;系统 Python PEP668,pip 加 `--break-system-packages`
- **代理**:港美股(yfinance)走 7890(`source /opt/clash/proxy.sh`);国内源(akshare/efinance)no_proxy 直连
- **baostock** 是裸 TCP(不认 HTTP_PROXY);三复权回补默认免费代理池(`--proxy-mode free`，HTTP CONNECT/SOCKS，需 `PySocks`)，失败剔除换 IP;**查询超时也强制换 IP**(`BAOSTOCK_FETCH_TIMEOUT` 默认 60s，防 login 通过却卡在内部接收循环的坏代理);池自愈(耗尽重拉 `BAOSTOCK_REBOOTSTRAP_*`、死IP TTL `BAOSTOCK_DEAD_TTL`、常驻刷新 `BAOSTOCK_MAX_AGE`/`BAOSTOCK_MIN_ALIVE`);**代理池+rebootstrap 耗尽→直连兜底**(`BAOSTOCK_DIRECT_FALLBACK` 默认 on,IP 解封可用;`_MAX`/`_COOLDOWN` 限流,长通道 `maybe_refresh` 池回血后切回);源经 clash 拉、可外置 `data/proxy_sources.json`(`BAOSTOCK_SOURCE_PROXY`)
- **数据**在 `data/stockfu.db`(WAL);回测产物 `data/backtest/`(gitignore)
- **回测防未来函数**:取数 `<= as_of`
- **正式回测价格口径**(`quote_snapshot`)：raw成交/盯市 + 公司行为账本；qfq仅限经白名单验证的尺度不变信号；hfq只作数据对账，不得用于正式账户。完整迁移见 `docs/BACKTEST.md`。
- **量化四层**:算子(math 连续 score)+策略 yaml+rebalancer+engine;算子缓存 fingerprint 含源码 hash
- **行情拆表**:QuoteSnapshot / EtfQuoteDaily / IndexQuoteDaily;`quote_model_for` 路由
- **入库统一收口 + 日期驱动**(`stockfu/services/quote_writer.py`):三张行情表各 1 个 canonical writer(`upsert_quote_snapshot`/`upsert_etf_daily`/`upsert_index_daily`),**严禁别处 `s.add(QuoteSnapshot...)`**;writer 硬保证 `quote_date <= cap_date`(超 cap 的源 bar 一律丢弃)。`--fetch` **必带 `--date YYYY-MM-DD`**,非法(未来/当日未收盘[北京16:00]/非交易日)→ `validate_ingest_date` 报错退出(凌晨不再误判为未开盘的今天);`--schedule` 自动取已收盘最近交易日。stamp 表(资金流/三层情绪/板块资金流)与读窗(composite/fundflow series)统一 `as_of=target_date`
- **官方 K 回补串行** `backfill_kline`;`--fetch` 只刷自选,不全市场;**A 股个股 `--fetch` 走 baostock 三复权**(全字段 + 当日 `close_raw`,baostock 全失败即放弃、**不降级东财/腾讯**;ETF→akshare、港美股→yfinance、指数→manager)
- **邮件出图(每日行情卡片)**:`run_mail_job()` = 出图(`render_share_images`:playwright 截 `/share` 多图 9:16,暖白·琥珀主题,chromium 元素截图 emoji 彩色)+ 发信(`send_card_email`:SMTP 465 走 SSL / 否则 STARTTLS,多图 inline 进一封 HTML)。数据走 `GET /share`→`share.build_card()`,**仅公开字段脱敏**(无持仓数/成本/盈亏);内容=上证/创业板/科创50 + ~90 行业(总览热力图+明细),**邮件不含个股持仓页**(截图前删含 `.sc-tbl` 的页;前端手动导出仍带个股)。触发:`--schedule` 到点 fetch→出图→发信(内嵌 uvicorn 单进程);`--test-mail` 手动(需 `--serve`)。门禁:`is_mail_ready`(账号/授权码/收件人)+ `export_readiness`(自选+3指数须同属最近交易日,按品种路由行情表;不同日→拒绝出图,宁可不发)。依赖 `playwright`+chromium;**坑**:`--test-mail` 出图时主页 `loadAll` 会 spin 起 baostock 代理池挂死 → 必加 `BAOSTOCK_PROXY_MODE=direct`(`--schedule` 内嵌渲染不等 loadAll,无此患)
- **代码检查**:`pyproject.toml [tool.ruff]` 已配,基线只启用 `F`(未用 import/变量/未定义名/无占位符 f-string),专防 6219e10 那类「重构后名字作用域错位」的 NameError 回归;默认规则集另有 16 个 E 类遗留(E741 模糊变量名 `l` / E702 / E701 / E402,均非 bug),清理后再 `select=["E","F"]`。改完代码顺手 `ruff check`

## 状态
🚧 MVP。代码:数据层(qfq 硬化)+回测四层+横截面策略族+全周期 CLI+荐股+三复权字段。  
历史回测产物只作探索记录；在 `docs/BACKTEST.md` 的正式准入门禁通过前，不得据此判断策略优劣。
