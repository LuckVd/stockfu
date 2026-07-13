# 项目路线图

本文件是项目总体技术设计与长期进度的唯一事实来源。

使用原则：

- 总体技术方向、阶段规划、目标拆分、依赖关系统一写在这里。
- 当前正在执行的目标细节写在 `goals/<id>.md`（多目标可并行），不要把步骤级执行过程堆到本文件。
- 路线图中的目标和子目标必须使用稳定编号，便于依赖跟踪和同步。
- 目标完成后的实现结果、测试结果、提交记录应回写到本文件表格中。

> 本文件与 `docs/PROJECT_STATE.md`（冷启动手册/工作日志）分工：路线图写稳定架构与长期目标，PROJECT_STATE 写运行命令、数据现状、调试坑、近期工作日志。两者不互相复制步骤级细节。

## 1. 项目概述

**StockFu · 资产管理终端**：本地优先的综合资产 + 市场情绪终端。

覆盖 A股/港股/美股/ETF 持仓管理、TTM 股息率与股息率网格、三层（市场/板块/个股）fear-greed-heat 情绪指数、历史回补、AI 4 顾问、天级回测（算子→策略→选股→执行 四层架构）。技术栈：Python + SQLModel/SQLite + textual TUI + FastAPI + Vue3 前端。

**当前阶段**：MVP 开发中。数据层（多源 fallback）→ 存储 → 持仓/股息/网格 → TUI → API → 三层情绪指数 → 历史回补 → AI 4 顾问 → 四层架构回测引擎均已落地；下阶段重点在**数据缺口补全**（激活回测基准、估值因子）、**板块轮动信号**与**TUI 多屏**。

## 2. 总体技术架构

5 层架构，自底向上：数据层 → 存储层 → 服务层 → AI/回测层 → 接口层，统一入口 `main.py`。

### 2.1 核心模块

| 模块ID | 模块名称 | 职责 | 关键接口/输入输出 | 备注 |
|---|---|---|---|---|
| M01 | 入口 `main.py` | 统一 CLI 入口 | `--init-db/--buy/--backfill*/--fetch/--backtest/--serve/--schedule/--test-mail`，默认 TUI | |
| M02 | 数据层 `data/` | 多源抓取 + fallback | `DataProviderManager` 7 源 fallback：efinance/tencent/sina/pytdx/baostock(含估值 peTTM/pbMRQ)/akshare/yfinance；市场识别/代码标准化/熔断器/TTL 缓存 | 代理分流：港美股走 7890，国内源 no_proxy 直连 |
| M03 | 存储层 `db.py`+`models.py` | SQLModel engine + 开发期迁移 + seed | 单文件 `data/stockfu.db`；模型覆盖 Asset/Transaction/Holding/Dividend/QuoteSnapshot/Index/Factor/FundFlow/Sector 等 | 搬迁 = 拷贝单文件 |
| M04 | 服务层 `services/` | 业务计算 | factors(历史分位) / market_data(宏观因子) / composite(三层情绪合成) / portfolio(持仓汇总) / dividend / grid / fundflow / sentiment / trading(移动加权) / backfill(历史回补) | 情绪指数：多因子→历史分位→等权 |
| M05 | AI 层 `ai/` | 实盘 4 顾问 + 算子平台 + 选股层 | 4 常驻顾问(趋势/逆向/风险/估值)；operators(7 math + 4 llm + 2 聚合)；rebalancers(pass_through/cap_and_rank/top_n_picker) | active 走 `app_config.active_strategy_id`/`active_rebalancer_id` |
| M06 | 回测层 `backtest/` | 天级回测引擎 | `engine.py`: VirtualAccount + T+1 开盘 + 真实费用 + 完整 metrics；四层：算子→策略→rebalancer→执行 | 防未来函数：取数 `<= as_of` |
| M07 | 算子缓存 | 跨回测复用算子结果 | `operator_result` 表：同 `(code, as_of, fingerprint)` 全局命中 | 首次回测慢(算+写)，后续秒级 |
| M08 | 调度层 `scheduler/` | 定时任务 | `run_daily_job`(行情+分红+ETF+三层指数) / backfill_kline / ensure_stock_data_and_index / APScheduler 定时 | `--schedule` daemon 可内嵌 web |
| M09 | 接口层 `api/`+`tui/` | 对外呈现 | FastAPI(`/portfolio /quote /dividend /grid /indices/* /fundflow /sentiment`)；textual TUI 看板 + 交易录入模态 | |

### 2.2 关键集成关系

- **抓取→情绪**：`data/manager.py`(多源 fallback) → `services/*`(各因子) → `services/composite.py`(三层 fear/greed/heat 合成) → `index_snapshot` 表(三 scope: market/sector/stock)。
- **回测数据流**：`scheduler.run` 注入 `CompiledStrategy` → 算子执行(命中 `operator_result` 缓存) → rebalancer 选股 → `backtest/engine.py` T+1 执行 → metrics。CLI `--backtest` 与 scheduler 同路径。
- **加个股联动**：TUI/API 加个股 → 后台 `ensure_stock_data_and_index`(补历史 K线 + 算该股三层情绪指数)。
- **外部依赖**：代理 mihomo:7890(港美股必需)；7 个免费数据源 efinance/tencent/sina/pytdx/baostock(估值)/akshare/yfinance（各自限流/反爬，多源互为 fallback；估值 PE/PB 历史由 baostock 提供，无需付费源）。

## 3. 设计约束

硬约束与必须遵守的工程规则：

- **防未来函数**：所有取数带 `<= as_of` 上界；信号用 T-1 数据、T+1 开盘执行。`build_context(code, as_of=None)` / `ma_alignment(code, lookback, as_of=None)` 支持 as_of，实盘不传 = 取最新。
- **四层量化架构边界**：算子→策略→rebalancer→执行，不跳层；active 策略/选股器走 `app_config` 路由，不硬编码。
- **算子缓存契约**：同 `(code, as_of, fingerprint)` 全局复用，勿绕过缓存重复算；算子签名变更必须更新 fingerprint。
- **单文件 SQLite**：全部数据在 `data/stockfu.db`（搬迁 = 拷贝；已备份 `.bak.*`）；回测产物在 `data/backtest/`（已 gitignore，运行时产物）。
- **运行环境**：Python 用 `python3`（`python` 不存在）；pip 加 `--break-system-packages`（系统 Python PEP668）。

> 项目专属约束写在 `constraints/project.md`；工作流默认约束见 `constraints/global.md`。

## 4. 阶段目标

**阶段 P1（已完成）**：数据层多源 fallback → 存储 → 持仓/股息/网格 → TUI/API → 三层情绪指数 → 历史回补 → AI 4 顾问 → 四层架构回测引擎（防未来函数 + 算子缓存 + 完整 metrics）。

**阶段 P2（进行中，候选目标见第 5 节）**：
- 数据缺口补全：行情拆表 + 510300 ETF 历史激活回测基准；PE/PB 历史分位(tushare)；连板长期断点续传。
- 信号能力：板块轮动信号（连续 N 日净流入排名 / 板块间资金切换，历史地基已就位）。
- 体验：TUI 多屏（个股/板块情绪详情屏）；美股 quote_snapshot 抓取修复。
- LLM 回测：classic_4advisors/hybrid 策略回测（需 LLM key）。

## 5. 路线图进度表

- 每一行表示一个目标或子目标。主目标行填 `目标ID`，`子目标ID` 留空；子目标行同时填两者。
- `前置依赖` 引用已有编号，如 `G01` 或 `G01-S02`。
- `状态`：`planned` / `designing` / `in_progress` / `blocked` / `done` / `dropped`。
- `验收结果`：`pending` / `accepted` / `partial` / `failed`；`测试状态`：`not_started` / `in_progress` / `passed` / `failed`。
- `实现时间` 用 `YYYY-MM-DD`；`Commit ID` 仅真实提交后填写。

> 下表均为**候选目标**（`planned`），尚未锁定。由 `/ai-goal` 选定方向并确认方案后，相应目标进入 `designing`→`in_progress`，细节落到 `goals/<id>.md`。

| 目标ID | 子目标ID | 名称 | 描述 | 状态 | 前置依赖 | 风险/阻塞 | 验收结果 | 测试状态 | 实现时间 | Commit ID | 备注 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| G01 | — | 行情拆表 | ETF/指数独立成表，替换 `quote_model_for` 单表路由 | done | — | — | accepted | passed | — | — | 代码已完成(QuoteSnapshot 个股 / EtfQuoteDaily / IndexQuoteDaily 三表)，本次核实修正文档 |
| G02 | — | 回测基准激活 | ETF/指数行情数据已就位，接回测引擎基准取数路径，激活超额收益基准(当前 N/A) | planned | — | 引擎基准读取未接 etf/index_quote_daily | pending | not_started | — | — | 候选；510300(2021起)与指数历史已落库 |
| G03 | — | LLM 策略回测 | classic_4advisors / hybrid 策略可回测（纯 math 策略已可用） | planned | — | 需 LLM key | pending | not_started | — | — | 候选 |
| G04 | — | 估值窗口延至 10 年 | baostock 已提供 PE/PB 5 年历史(2021 起)，继续 backfill 延长至 10 年，匹配估值类分位窗口 | planned | — | baostock 个股历史深度受限 | pending | not_started | — | — | 候选；PE/PB 分位已可用(valuation.py) |
| G05 | — | 连板长期回补 | 多次 `--backfill-limit` 断点续传补连板/涨停长期序列 | planned | — | 东财 `stock_zt_pool_em` 限流 | pending | not_started | — | — | 候选；机械补数 |
| G06 | — | 板块轮动信号 | 连续 N 日净流入排名 / 板块间资金切换信号 | planned | — | 历史地基已就位(sector_flow/factor_snapshot) | pending | not_started | — | — | 候选 |
| G07 | — | TUI 多屏 | 个股/板块情绪详情屏 | planned | — | — | pending | not_started | — | — | 候选；体验向 |
| G08 | — | 美股 quote 修复 | 美股 quote_snapshot 抓取修复（AAPL 等为空） | planned | — | yfinance 取数/字段口径 | pending | not_started | — | — | 候选；bug 修复 |

## 6. 开放风险与阻塞

- **数据源依赖免费源**：akshare/efinance/yfinance 各有限流/反爬，稳定性受限（见 `docs/PROJECT_STATE.md` 第 7 节"已知数据坑"）。
- **估值窗口偏短**：PE/PB 历史仅 5 年(2021 起，baostock 已落库、`valuation.py` 分位已实现)，估值类分位理想需 10 年窗口（G04）。
- **回测基准未激活**：ETF/指数行情数据已就位，但回测引擎基准取数路径未接 → 基准常 N/A，超额收益指标受限（G02）。
- **北向/涨跌家数停服**：2024 起北向停服；涨跌家数东财全量反爬，宏观因子部分缺失。
- 步骤级实现问题放在 `goals/<id>.md` 的 `Blockers`，不在此记录。
