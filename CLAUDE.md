# StockFu · 资产管理终端

本地优先的综合资产 + 市场情绪终端:A股/港股/美股/ETF 持仓管理、TTM 股息率/网格、三层(市场/板块/个股)fear-greed-heat 情绪指数、历史回补、AI 4 顾问、天级回测(算子→策略→选股→执行 四层架构)。技术栈:Python + SQLModel/SQLite + textual TUI + FastAPI + Vue3 前端。

## 冷启动(新会话先读)
- **`docs/PROJECT_STATE.md`** — 工作日志/冷启动手册:一句话现状、5 层架构、运行命令、数据现状表、已知数据坑、待办。**先读这份接上下文**。
- `docs/ROADMAP.md` — 项目路线图(总体架构 + G0x 进度表 + 长期目标 + 开放风险)
- `docs/BACKTEST.md` — 回测引擎(四层架构 + scheduler + 算子缓存 + 完整 metrics)
- `docs/AI_ADVISORS.md` — 实盘 AI 4 顾问(4 顾问同时作为 LLM 算子供回测)

## 运行命令
```bash
cd /opt/pro/stockfu
python3 main.py --init-db          # 建库 + 种子(自选/演示持仓/算子平台 operator+strategy)
python3 main.py                    # textual TUI 看板(默认)
python3 main.py --serve            # FastAPI(127.0.0.1:8787)
python3 main.py --fetch            # 每日抓取 + 算三层情绪指数落库
python3 main.py --backtest bollinger_monthly --start 2025-06-01 --end 2026-01-01 --codes 600519,000858 --save  # 回测(见 docs/BACKTEST.md)
nohup python3 main.py --schedule >> data/schedule.log 2>&1 &   # daemon:定时抓取 + 邮件 + 内嵌 web
```

## 关键约定(踩坑点)
- **Python 用 `python3`**(`python` 不存在);系统 Python PEP668,pip 装 包加 `--break-system-packages`
- **代理**:港美股(yfinance)走 7890(`source /opt/clash/proxy.sh`);国内源(akshare/efinance)no_proxy 直连
- **数据全在 `data/stockfu.db`**(SQLite 单文件,**WAL 模式**;搬迁=拷贝它——**WAL 下先 `PRAGMA wal_checkpoint(TRUNCATE)` 或一并拷 -wal/-shm**;已备份 `data/stockfu.db.bak.*`);回测结果在 `data/backtest/`(已 gitignore,运行时产物)
- **回测防未来函数**:取数严格 `<= as_of`;`build_context(code, as_of=None)` / `ma_alignment(code, lookback, as_of=None)` 都支持 as_of,实盘不传=取最新
- **量化平台四层**(`stockfu/ai/` + `stockfu/backtest/`):
  - 算子 `operators/`(9 math + 2 聚合,共 11;**回测 LLM 算子已下线 G10**,实盘 AI 4 顾问走 `ai/skills/` 独立链路)。**G10 后**:算子 score 连续不 clamp,signal 降级为派生标签(不参与决策),仓位统一 continuous 映射(满仓刻度 `score_full`,详见 BACKTEST.md §8)
  - 策略 `strategies/*.yaml` + DB strategy 表(6 个纯 math,含 `cn_momentum_rotation` 已全周期证伪;active 走 `app_config.active_strategy_id`,默认 `pure_factor`)
  - 选股 `rebalancers/`(pass_through / cap_and_rank / top_n_picker;active 走 `app_config.active_rebalancer_id`)
  - 执行 `backtest/engine.py`(VirtualAccount + T+1 开盘 + 真实费用 + 完整 metrics)+ `scheduler.py`(注入 CompiledStrategy + 算子缓存)
- **算子缓存** `operator_result` 表:同 `(code, as_of, fingerprint)` 全局复用(fingerprint 含算子源码 hash → 改算子代码自动失效缓存,治 P2-5;G10),首次回测慢(算+写),后续读缓存秒级。**G09 性能**:复合唯一键覆盖热路径(4 单列索引已删)+ meta 进程级 `lru_cache` + WAL/`synchronous=NORMAL`/`busy_timeout`;`python3 main.py --vacuum`(停 daemon 时跑)回收空闲页
- **行情已拆表**:`QuoteSnapshot`(个股,含 pe/pb/ps_ttm/pcf) / `EtfQuoteDaily` / `IndexQuoteDaily` 三表分离;`quote_model_for` 按类型路由(个股→QuoteSnapshot / ETF→EtfQuoteDaily / 指数→IndexQuoteDaily);**回测基准 G02 已激活**:基准=上证综指 sh000001(1990起,`index_quote_daily`),`_benchmark_curve` 直读不走 `quote_model_for`;`run_scheduled_fetch` 每日更新;`--backfill-benchmark` 全量回补

## 状态
🚧 MVP 开发中。已完成:数据层(多源 fallback)→ 存储 → 持仓/股息/网格 → TUI → API → 三层情绪指数 → 历史回补 → AI 4 顾问 → 四层架构回测引擎。待办见 `docs/PROJECT_STATE.md` 第 8 节。
