# StockFu · 资产管理终端 (stockfu)

> 本地优先的**综合资产管理 + 市场情绪监控 + A股策略研究**终端。看板走 **Vue3 Web + FastAPI**，运维/研究走 **CLI**；数据层采用多数据源 fallback。

## 它解决什么

StockFu 是一个面向个人的资产 + 市场情绪 + 策略研究终端，覆盖：

- **资产追踪**：A股 / 港股 / 美股 / 基金 / ETF，~30 只个股规模
- **红利能力**：分红 / TTM 股息率追踪、收益里程碑（已回本 / 年红利）
- **自定义指数**：恐慌指数、热度指数，带**天级历史**
- **板块情绪 / 历史分析**（各板块）
- **大资金流向**：从宽基 / 行业 ETF 份额变化追踪
- **A股策略研究**：横截面因子算子平台 + 回测引擎 + 三跑门禁验证体系（`docs/BACKTEST.md`）
- **自选股评价矩阵**：通用股票评价引擎，多策略交叉评价自选池（`--watchlist-review`）
- **每日策略评分**：沪深300+中证500成分全量因子落库，各策略独立输出 0–100 分；逐股选择邮件与按需 LLM（见 `docs/SPECS/signal-recommendation-mail.md`）
- 借鉴：AI 决策报告、财经新闻、消息推送

## 设计思想

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
├── main.py              # 入口：默认 Web / --init-db / --fetch / --backtest / --watchlist-review …
├── stockfu/
│   ├── config.py        # 配置（.env）
│   ├── db.py            # SQLModel engine / 建表 / 迁移
│   ├── models.py        # 资产/持仓/交易/分红/指数/资金流/新闻/策略/算子
│   ├── data/            # 数据层（多源 fallback + 熔断 + 缓存）
│   ├── services/        # 业务（持仓汇总/股息率/历史宇宙/公司行为）
│   ├── api/             # FastAPI 路由 + 静态前端
│   ├── scheduler/       # 每日抓取/落库/推送
│   ├── ai/
│   │   ├── skills/      # AI 4 顾问（独立链路）
│   │   ├── operators/   # 算子平台：factors（20+ 横截面因子）+ aggregators + 策略 seed
│   │   ├── strategies/  # 策略 YAML（单一真源，cap_and_rank 横截面为主）
│   │   └── rebalancers/ # 选股/仓位层（cap_and_rank / pass_through）
│   └── backtest/        # 回测引擎（T+1 执行、raw/qfq 口径、operator_cache、三跑门禁）
├── frontend/            # Vue3 Web 看板
├── strategy_specs/      # 策略规格文档（7 个已落地规格 + 2026-08 调研候选）
└── data/stockfu.db      # SQLite（运行时生成；operator_cache.db 为算子缓存库）
```

## 快速开始

```bash
# 依赖安装（生产/验证统一用锁文件；uv 未装时先 pip install uv）
UV_BREAK_SYSTEM_PACKAGES=1 uv pip install --system -r requirements.lock
playwright install chromium   # 邮件分享卡片渲染需要（依赖升级后需重装浏览器二进制）
python3 main.py --init-db     # 初始化 + 种子自选 + 算子/策略注册
python3 main.py               # 启动 Web（默认 127.0.0.1:8787）
python3 main.py --serve       # 同上
python3 main.py --fetch --date 2026-07-22   # 日更抓取（必带 --date；未来/未收盘/非交易日报错）

# 前端开发/构建
cd frontend && pnpm install && pnpm dev
cd frontend && pnpm build    # 生成被 FastAPI 托管的 frontend/dist

# 回测（研究模式；--valuation-basis raw = 不复权+现金分红入账）
python3 main.py --backtest low_beta_dividend --start 2007-01-04 --end 2026-07-21 --valuation-basis raw
python3 main.py --factor-diag low_beta --params '{"window":120}'   # 单因子诊断（IC/分位收益）
python3 main.py --watchlist-review --no-llm                          # 自选股多策略评价矩阵
python3 main.py --scan-signals --date 2026-08-04                     # 刷新800只成分并运行配置策略
python3 main.py --test-signal-mail                                   # 发送最近批次推荐卡片
python3 main.py --schedule                                           # 市场日报 + 每日策略评分调度
```

## 状态

🔄 研究模式推进中。已完成：数据层(多源 fallback) → 存储 → 持仓/股息业务 → Web/API → 三层情绪指数 → 历史回补 → AI 4 顾问 → 四层架构回测引擎 → **20+ 横截面因子算子 + 调研候选策略 + 三跑门禁体系（2026-08）**。

**回测研究现状**（`docs/BACKTEST.md` §0.6，raw 口径，基准沪深300，2007–2026）：
- 自家红利横截面系（`dividend_cross_section` base/融合）仍是现有基线；具体数字以同口径回测产物为准
- 2026-08 已完成 10 个调研策略 canonical full；其中 7/10 总收益高于沪深300，但修正配置后的样本外三跑尚未完成。`smart_beta_multi_factor` 经持仓排查降级为风格参照（train 段超额主要是小盘 beta）
- 第二批量价研究模板与第一批修正配置后的子样本验证进行中
- 纪律：**任何新策略/改参数后的结论必须过三跑门禁**（全样本 + 两段 ≥5 年子样本）；长窗回测结果均为研究模式定位——趋势/方向可信，绝对数字允许误差

TUI 终端看板已移除，交互统一走 Web。回测的准确性缺口、目标架构、实施顺序和验收门禁统一见 [`docs/BACKTEST.md`](docs/BACKTEST.md)。
