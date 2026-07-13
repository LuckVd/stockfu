# StockFu · 资产管理终端

本地优先的综合资产 + 市场情绪终端:A股/港股/美股/ETF 持仓管理、TTM 股息率/网格、三层(市场/板块/个股)fear-greed-heat 情绪指数、历史回补、AI 4 顾问、天级回测(算子→策略→选股→执行 四层架构)。技术栈:Python + SQLModel/SQLite + textual TUI + FastAPI + Vue3 前端。

## 冷启动(新会话先读)
- **`docs/PROJECT_STATE.md`** — 工作日志/冷启动手册:一句话现状、5 层架构、运行命令、数据现状表、已知数据坑、待办。**先读这份接上下文**。
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
- **数据全在 `data/stockfu.db`**(SQLite 单文件,搬迁=拷贝它;已备份 `data/stockfu.db.bak.*`);回测结果在 `data/backtest/`(已 gitignore,运行时产物)
- **回测防未来函数**:取数严格 `<= as_of`;`build_context(code, as_of=None)` / `ma_alignment(code, lookback, as_of=None)` 都支持 as_of,实盘不传=取最新
- **量化平台四层**(`stockfu/ai/` + `stockfu/backtest/`):
  - 算子 `operators/`(7 math + 4 llm + 2 聚合,共 13)
  - 策略 `strategies/*.yaml` + DB strategy 表(6 个;active 走 `app_config.active_strategy_id`)
  - 选股 `rebalancers/`(pass_through / cap_and_rank / top_n_picker;active 走 `app_config.active_rebalancer_id`)
  - 执行 `backtest/engine.py`(VirtualAccount + T+1 开盘 + 真实费用 + 完整 metrics)+ `scheduler.py`(注入 CompiledStrategy + 算子缓存)
- **算子缓存** `operator_result` 表:同 `(code, as_of, fingerprint)` 全局复用,首次回测慢(算+写),后续读缓存秒级
- **行情已拆表**:`QuoteSnapshot`(个股,含 pe/pb/ps_ttm/pcf) / `EtfQuoteDaily` / `IndexQuoteDaily` 三表分离,`quote_model_for` 按类型路由;510300 ETF(2021起)与指数历史已落库,但回测引擎基准取数路径未接 → 基准常 N/A(数据层就位,待 G02 激活)

## 状态
🚧 MVP 开发中。已完成:数据层(多源 fallback)→ 存储 → 持仓/股息/网格 → TUI → API → 三层情绪指数 → 历史回补 → AI 4 顾问 → 四层架构回测引擎。待办见 `docs/PROJECT_STATE.md` 第 8 节。
