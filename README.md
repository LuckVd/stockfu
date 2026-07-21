# StockFu · 资产管理终端 (stockfu)

> 本地优先的**综合资产管理 + 市场情绪监控**终端。看板走 **Vue3 Web + FastAPI**，运维/研究走 **CLI**；数据层复用 [daily_stock_analysis](../daily_stock_analysis) 的多数据源 fallback 思想。

## 它解决什么

StockFu 是一个面向个人的资产 + 市场情绪终端，覆盖：

- **资产追踪**：A股 / 港股 / 美股 / 基金 / ETF，~30 只个股规模
- **红利能力**：分红 / TTM 股息率追踪、股息率网格买卖计划、收益里程碑（已回本 / 年红利）
- **自定义指数**：恐慌指数、热度指数，带**天级历史**
- **板块情绪 / 历史分析**（各板块）
- **大资金流向**：从宽基 / 行业 ETF 份额变化追踪
- 借鉴：AI 决策报告、财经新闻、消息推送

## 设计思想（来自 daily_stock_analysis）

| 思想 | 本项目落点 |
|------|-----------|
| 多数据源 + 自动降级（**前复权**） | `stockfu/data/manager.py`：baostock→efinance→tencent→akshare→yfinance（已删 sina/pytdx 不复权源） |
| 熔断器（连续失败熔断冷却） | `stockfu/data/base.py` |
| 分红专门抓 + TTM 股息率 | A股走 akshare `stock_fhps_detail_em`；港美股走 yfinance `dividends` |
| 市场识别 + 代码标准化 | `stockfu/data/base.py` |
| 配置驱动、不配也能跑 | `stockfu/config.py` |

## 目录结构

```
stockfu/
├── main.py              # 入口：默认 Web / --init-db / --fetch / --backtest …
├── stockfu/
│   ├── config.py        # 配置（.env）
│   ├── db.py            # SQLModel engine / 建表 / 种子
│   ├── models.py        # 资产/持仓/交易/分红/指数/资金流/新闻
│   ├── data/            # 数据层（多源 fallback + 熔断 + 缓存）
│   ├── services/        # 业务（持仓汇总/股息率/网格）
│   ├── api/             # FastAPI 路由 + 静态前端
│   ├── scheduler/       # 每日抓取/落库/推送
│   ├── ai/              # AI 4顾问 + operators算子平台 + rebalancers选股层
│   └── backtest/        # 回测引擎(四层架构,见 docs/BACKTEST.md)
├── frontend/            # Vue3 Web 看板
└── data/stockfu.db      # SQLite（运行时生成）
```

## 快速开始

```bash
pip install -r requirements.txt
python3 main.py --init-db     # 初始化 + 种子自选
python3 main.py               # 启动 Web（默认 127.0.0.1:8787）
python3 main.py --serve       # 同上
python3 main.py --fetch       # 日更抓取
```

## 状态

🚧 MVP 开发中。已完成：数据层(多源 fallback) → 存储 → 持仓/股息业务 → Web/API → 三层情绪指数 → 历史回补 → AI 4 顾问 → 四层架构回测引擎(算子缓存+选股+真实费用)。  
TUI 终端看板已移除，交互统一走 Web。详见 `docs/PROJECT_STATE.md`。
