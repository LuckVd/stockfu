# StockFu 路线图

项目总体技术设计与长期进度的单一事实来源。运行命令、数据现状、调试坑、近期工作日志见 [`PROJECT_STATE.md`](./PROJECT_STATE.md)；冷启动约定见根目录 `CLAUDE.md`。本文只写稳定架构与长期目标，不堆步骤级执行细节。

## 1. 概述

**StockFu · 资产管理终端**：本地优先的综合资产 + 市场情绪终端。覆盖 A股/港股/美股/ETF 持仓管理、TTM 股息率与股息率网格、三层（市场/板块/个股）fear-greed-heat 情绪指数、历史回补、AI 4 顾问、天级回测（算子→策略→选股→执行 四层架构）。技术栈：Python + SQLModel/SQLite + textual TUI + FastAPI + Vue3 前端。

**当前阶段**：MVP。数据层（多源 fallback）→ 存储 → 持仓/股息/网格 → TUI/API → 三层情绪指数 → 历史回补 → AI 4 顾问 → 四层架构回测引擎均已落地；回测基准已激活（G02）；**回测性能优化（G09）已完成**；**回测做减法（G10，2026-07-15）已完成**——砍回测 LLM 算子 + 铲 ±20/signal 体系，回测变纯连续因子；**因子诊断层（阶段2，2026-07-15）已完成**——单算子 IC/分位收益/换手/衰减。下阶段重点：**执行层抽象（阶段3）**、数据缺口补全、板块轮动信号与 TUI 多屏（见 §5）。

## 2. 总体技术架构

5 层架构，自底向上：数据层 → 存储层 → 服务层 → AI/回测层 → 接口层，统一入口 `main.py`。

| 模块 | 职责 | 关键点 |
|---|---|---|
| `main.py` | 统一 CLI 入口 | `--init-db/--buy/--backfill*/--fetch/--backtest/--serve/--schedule/--test-mail`，默认 TUI |
| `data/` 数据层 | 多源抓取 + fallback | `DataProviderManager` 7 源：efinance/tencent/sina/pytdx/baostock(含估值)/akshare/yfinance；代理分流：港美股走 7890，国内源 no_proxy 直连 |
| `db.py`+`models.py` 存储层 | SQLModel engine + 开发期迁移 + seed | 单文件 `data/stockfu.db`；QuoteSnapshot/EtfQuoteDaily/IndexQuoteDaily 三表分离；搬迁 = 拷贝单文件 |
| `services/` 服务层 | 业务计算 | factors(历史分位)/market_data(宏观)/composite(三层情绪合成)/portfolio/dividend/grid/fundflow/sentiment/trading(移动加权)/backfill |
| `ai/` AI 层 | 实盘 4 顾问 + 算子平台 + 选股层 | 4 常驻顾问(趋势/逆向/风险/估值，实盘走 skills/)；operators(8 math + 2 聚合，回测 LLM 已下线)；rebalancers(pass_through/cap_and_rank/top_n_picker)；active 走 `app_config` |
| `backtest/` 回测层 | 天级回测引擎 | `engine.py`: VirtualAccount + T+1 开盘 + 真实费用 + 完整 metrics；防未来函数：取数 `<= as_of` |
| 算子缓存 | 跨回测复用算子结果 | `operator_result` 表：同 `(code, as_of, fingerprint)` 全局命中；首次慢，后续秒级 |
| `scheduler/` 调度层 | 定时任务 | `run_daily_job`(行情+分红+ETF+三层指数)/backfill_kline/ensure_stock_data_and_index；`--schedule` daemon 可内嵌 web |
| `api/`+`tui/` 接口层 | 对外呈现 | FastAPI；textual TUI 看板 + 交易录入模态 |

**关键数据流**：`data/manager.py`(多源) → `services/*`(各因子) → `services/composite.py`(三层 fear/greed/heat 合成) → `index_snapshot` 表。回测：`scheduler.run` 注入 `CompiledStrategy` → 算子执行(命中 `operator_result`) → rebalancer 选股 → `engine.py` T+1 执行 → metrics。

## 3. 设计硬约束

详细约定见 `CLAUDE.md`「关键约定」。核心红线：

- **防未来函数**：取数严格 `<= as_of`；信号用 T-1、T+1 开盘执行。`build_context`/`ma_alignment` 支持 `as_of`，实盘不传 = 取最新。
- **四层量化架构边界**：算子→策略→rebalancer→执行，不跳层；active 走 `app_config` 路由，不硬编码；算子源码变更自动失效缓存(fingerprint 含 source hash，见 `operator_cache.compute_fingerprint`)。
- **单文件 SQLite**：全部数据在 `data/stockfu.db`（勿引入第二个库）；回测产物落 `data/backtest/`（gitignore，勿入库）。
- **运行环境**：Python 用 `python3`；pip 加 `--break-system-packages`（系统 Python PEP668）。

## 4. 进度表

状态：✅完成 / ⏳进行中 / 📋计划。实现时间 `YYYY-MM-DD`；Commit ID 仅真实提交后填。

| ID | 名称 | 描述 | 状态 | 依赖 | 结果 | 实现时间 | Commit | 备注 |
|---|---|---|---|---|---|---|---|---|
| G01 | 行情拆表 | ETF/指数独立成表，替换 `quote_model_for` 单表路由 | ✅ | — | 已验收 | — | — | QuoteSnapshot/EtfQuoteDaily/IndexQuoteDaily 三表已完成 |
| G02 | 回测基准激活 | 接回测引擎基准取数路径，激活超额收益基准 | ✅ | — | 已验收 | 2026-07-14 | 775e881 | 基准=上证综指 sh000001(1990起)，`_benchmark_curve` 直读 `index_quote_daily`，`run_scheduled_fetch` 每日更新 |
| G03 | ~~LLM 策略回测~~ | classic_4advisors / hybrid 策略可回测 | ❌废弃 | — | — | — | — | G10 砍回测 LLM 算子,两策略已删;实盘 AI 4 顾问保留(独立链路 skills/) |
| G04 | 估值窗口延至 10 年 | baostock PE/PB 5 年历史(2021 起)继续 backfill 延长至 10 年 | 📋 | — | — | — | — | baostock 个股历史深度受限；PE/PB 分位已可用(valuation.py) |
| G05 | 连板长期回补 | 多次 `--backfill-limit` 断点续传补连板/涨停长期序列 | 📋 | — | — | — | — | 东财 `stock_zt_pool_em` 限流；机械补数 |
| G06 | 板块轮动信号 | 连续 N 日净流入排名 / 板块间资金切换信号 | 📋 | — | — | — | — | 历史地基已就位(sector_flow/factor_snapshot) |
| G07 | TUI 多屏 | 个股/板块情绪详情屏 | 📋 | — | — | — | — | 体验向 |
| G08 | 美股 quote 修复 | 美股 quote_snapshot 抓取修复（AAPL 等为空） | 📋 | — | — | — | — | yfinance 取数/字段口径 |
| G09 | 回测性能优化 | operator meta 进程级 lru_cache + 删冗余单列索引 + WAL；热缓存纯读回测提速 | ✅ | — | 已验收 | 2026-07-15 | — | meta lru_cache + 删 4 单列索引(复合唯一键覆盖热路径)+ WAL/synchronous=NORMAL/busy_timeout + `--vacuum` 工具；优化前后 metrics 逐值一致(防未来函数红线通过)。详见 [G09-backtest-perf.md](./G09-backtest-perf.md) |
| G10 | 回测做减法 | 砍回测 LLM 算子 + 铲 ±20/signal → 纯连续因子；OpResult 瘦身 + 缓存源码 hash(治 P2-5) | ✅ | — | 已验收 | 2026-07-15 | — | operators/llm/ 删 + hybrid/classic_4advisors 废弃;算子 score 连续不 clamp、signal 降级派生、continuous 满仓锚点 score_full 参数化、OpResult 13→10 字段;指纹含 source hash。实盘 AI 4 顾问不动。属行为改变类,口径见 BACKTEST.md §8 |

## 5. 下一阶段（P2 候选）

**回测演进（G10 后优先）**：
- **阶段2 · 因子诊断层 ✅ 已完成（2026-07-15）**：alphalens 思路——单算子连续 `score`（已不 clamp）算 IC / 分位收益 / 换手 / 衰减，验证单个因子**不必搭整条策略管道**。`backtest/factor_diag.py`（纯 Python 统计）+ CLI `--factor-diag <operator>`（`--codes all`=全市场 801 票），复用回测算子缓存（指纹逐字一致、跨场景互通）。补因子研究工作流缺口。详见 `docs/BACKTEST.md` §11 + `PROJECT_STATE.md` §8。
- **阶段3 · 执行层抽象**：见 `docs/ARCHITECTURE_REVIEW.md` §4 P2 清单——Broker(回测/实盘共用 P2-2)/Sizer-CommInfo(P2-3)/Analyzer 可组合(P2-4)/Position 开平分解(P2-8)/math 向量化 run_batch(P2-1/P2-7 治冷启动慢)。建议顺序 P2-1/7→P2-8/4→P2-3/2（P2-5 已于 G10 完成）。

**数据/信号/体验**：
- **数据缺口补全**：行情拆表(G01)+ 回测基准(G02)已完成；PE/PB 历史分位延至 10 年(G04)；连板长期断点续传(G05)。
- **信号能力**：板块轮动信号——连续 N 日净流入排名 / 板块间资金切换（G06，历史地基已就位）。
- **体验**：TUI 多屏——个股/板块情绪详情屏（G07）；美股 quote_snapshot 抓取修复（G08）。
- ~~**LLM 回测**~~：G03 已于 G10(2026-07-15)废弃——砍回测 LLM 算子，classic_4advisors/hybrid 删除；实盘 AI 4 顾问保留。
- **性能**：G09 回测性能优化（已完成，2026-07-15）。

## 6. 开放风险与阻塞

- **数据源依赖免费源**：akshare/efinance/yfinance 各有限流/反爬，稳定性受限（见 `PROJECT_STATE.md` 第 7 节）。
- **估值窗口偏短**：PE/PB 历史仅 5 年(2021 起，baostock 已落库、`valuation.py` 分位已实现)，估值类分位理想需 10 年窗口（G04）。
- **回测基准已解决（G02）**：基准 = 上证综指 sh000001（IndexQuoteDaily 1990 起），`_benchmark_curve` 直读不走 `quote_model_for`，超额收益指标恒定产出。
- **北向/涨跌家数停服**：2024 起北向停服；涨跌家数东财全量反爬，宏观因子部分缺失。

## 7. 已完成：G09 回测性能优化（2026-07-15）

G09 已完成：operator meta 进程级 `lru_cache` + 删 4 冗余单列索引（复合唯一键覆盖热路径）+ WAL/`synchronous=NORMAL`/`busy_timeout` + `--vacuum` 维护工具。回归验证优化前后 metrics + `equity_curve` + `trades` 逐字节一致（防未来函数红线通过）。完整诊断、方案、实施记录与 5 个待确认事项结论见 [G09-backtest-perf.md](./G09-backtest-perf.md)。

G10 同步完成 P2-5（算子指纹纳入源码 hash，治"改算子不 bump version → 旧缓存命中"坑）。下一阶段见 §5：回测演进优先（**阶段2 因子诊断层** / **阶段3 执行层抽象** = `docs/ARCHITECTURE_REVIEW.md` §4 的 P2-1/7/8/4/3/2），以及数据/信号/体验（板块轮动 G06 / TUI 多屏 G07 / 美股 quote G08 / 估值窗口 G04）。
