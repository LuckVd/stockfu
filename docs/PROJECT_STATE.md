# stockfu 项目状态（工作日志 / 冷启动手册）

> 新会话先读这份，就能接上。项目：`/opt/pro/stockfu/`。
> 定位：**StockFu·资产管理终端**，借鉴 `../daily_stock_analysis` 的多数据源 fallback 思想，TUI 为主 + FastAPI。

## 1. 一句话现状
一个本地优先的综合资产管理 + 市场情绪终端：持仓管理、股息/网格、**三层(市场/板块/个股) fear/greed/heat 情绪指数**、历史回补。SQLite 存储，textual TUI + FastAPI。

## 2. 架构（5 层）
```
stockfu/
├── main.py                    # 统一入口(--init-db/--buy/--backfill/--fetch/--serve/默认TUI)
├── stockfu/
│   ├── config.py              # pydantic-settings + setup_network(代理自动化)
│   ├── db.py                  # SQLModel engine + _migrate(开发期迁移) + seed
│   ├── models.py              # Asset/Transaction/Holding/DividendEvent/QuoteSnapshot
│   │                          #   /IndexSnapshot(三层scope)/FactorSnapshot/FundFlowSnapshot/NewsItem
│   ├── data/                  # 数据层(借鉴 daily_stock_analysis)
│   │   ├── base.py            # 市场识别/代码标准化/熔断器/TTL缓存/统一dataclass
│   │   ├── dividend_parser.py # 分红解析("派息"列/10, TTM)
│   │   ├── efinance_source.py # A股行情主力+日K(需传beg拉长历史)
│   │   ├── akshare_source.py  # A股分红(stock_history_dividend_detail)+资金流+实时兜底
│   │   ├── yfinance_source.py # 港美股行情+dividends+K线(period按days)
│   │   └── manager.py         # DataProviderManager 多源fallback
│   ├── services/
│   │   ├── factors.py         # 历史分位(估值类10年/情绪类5年窗口)
│   │   ├── market_data.py     # 宏观因子(涨跌家数/连板/两融/北向/ERP/股东人数)
│   │   ├── composite.py       # 三层 fear/greed/heat 合成(多因子分位等权)+SECTOR_MAP
│   │   ├── indices.py         # 旧单因子fear/heat(保留fallback)
│   │   ├── portfolio.py       # 持仓汇总(市值/盈亏/股息率/年红利/回本)
│   │   ├── dividend.py        # 分红落库
│   │   ├── grid.py            # 股息率网格买卖计划
│   │   ├── fundflow.py        # ETF成交额活跃度(份额免费源无,用成交额代理)
│   │   ├── sentiment.py       # 板块情绪温度
│   │   ├── trading.py         # 交易录入(移动加权平均)
│   │   └── backfill.py        # 历史回补(两融总量/连板/个股两融/股息率序列)
│   ├── scheduler/jobs.py      # run_daily_job(行情+分红+ETF+三层指数) + backfill_kline + ensure_stock_data_and_index(加个股即算) + schedule
│   ├── api/{server,routes}.py # FastAPI
│   └── tui/{app,trade_screen}.py  # textual 看板 + 交易录入模态屏
└── data/stockfu.db               # SQLite(全部历史,单文件可搬迁)
```

## 3. 已完成功能
- **资产管理**：持仓(移动加权平均)、TTM股息率、股息率网格买卖、收益里程碑(年红利/回本年限)
- **三层情绪指数**：fear/greed/heat × 市场/板块/个股。CNN式：多因子→历史分位(0-100)→等权平均
- **历史回补**：K线5年/两融总量11年/股息率5年序列/个股两融10天/连板(东财限流)
- **TUI**：持仓看板(含个股恐慌/贪婪/热度三列) + 顶部市场fear/greed/heat(分档着色) + 按b/s交易录入；加个股自动后台补历史K线+算该股三层情绪指数
- **API**：/portfolio /quote /dividend /grid /indices/{market,sector,stock,history} /fundflow /sentiment
- **代理自动化**：setup_network(港美股走7890代理,国内源no_proxy直连)

## 4. 数据现状（关键）
| 因子 | 历史 | 说明 |
|------|------|------|
| K线(波动/涨跌/成交/RS) | 5年(~1580条) | efinance A股传beg; yfinance period按days |
| 两融总量 | 11年(2000条) | stock_margin_sse 一次拉全 |
| 股息率序列 | 5年(6083条) | 本地算=TTM分红/价格,受分红事件历史限制 |
| 个股两融 | 近10天 | stock_margin_detail_sse(date)按日筛code |
| 连板/涨停 | 近3周(限流) | stock_zt_pool_em(date),东财批量限流,断点续传 |
| 分红事件 | 几年 | stock_history_dividend_detail "派息"列=每10股,/10 |
| PE/PB历史 | ❌无 | legu付费/csindex失效,需tushare(~200元) |
| 北向 | ❌无 | 2024起停服 |
| 涨跌家数 | ❌无 | legu不通/东财全量反爬 |

## 5. 运行命令
```bash
cd /opt/pro/stockfu && source /opt/clash/proxy.sh
python main.py --init-db              # 建库+种子自选+演示持仓
python main.py --buy 600519 100 1500 --date 2024-01-15   # 录入持仓(移动加权)
python main.py --backfill 1825        # K线补5年
python main.py --backfill-factors     # 两融总量+个股两融10天+股息率序列
python main.py --backfill-limit 365   # 连板(限流分批,断点续传,多次跑)
python main.py --fetch                # 每日抓取+算三层指数落库
python main.py --schedule             # APScheduler每日定时(默认工作日18:18)
python main.py                        # TUI看板
python main.py --serve                # API(127.0.0.1:8787)
```

## 6. 关键设计决策
- **情绪指数公式**：多因子→各历史分位→等权平均(CNN式)。fear=下行因子,greed=上行,heat=活跃
- **历史窗口**：估值类(PE/PB/股息率)10年,情绪/量价类5年。当前K线已5年
- **三层粒度**：index_snapshot(level=market/sector/stock, scope=MARKET/板块名/code)
- **代理**：mihomo 7890(港股/美股yfinance必须);国内源(akshare/efinance)no_proxy直连
- **代理切节点**：9090 API无密码,主组🚀节点选择,新加坡最稳(见memory mihomo-proxy-switching)

## 7. 已知数据坑（调试要点）
- akshare 1.18：`stock_margin_account_sse`不存在→用`stock_margin_sse`;`stock_a_indicator_lg`不存在(PE历史无免费源)
- `stock_margin_detail_sse(date)`只接受date参数,返回全市场需筛code;今日数据常缺,往前找交易日
- 东财反爬：`stock_zh_a_spot_em`(全量行情)/`stock_sector_fund_flow_rank`(板块)时不稳;`stock_zt_pool_em`批量限流→连板只能分批补
- efinance：`get_realtime_quotes`已坏(报"行情参数不正确");`get_quote_history`**必须传beg**才拉长历史(默认只近期)
- yfinance：港股符号`0700.HK`(4位补零);A股`600519.SS`;需代理;period按days选
- A股分红：`stock_history_dividend_detail`"派息"列是**每10股**,每股=/10
- 招行股息率偏高(13%):数据源税前口径,需校准
- pip：系统python PEP668,用`--break-system-packages`

## 8. 待办（P2 / 未来）
- AI决策报告(接LLM key,config已预留LLM_*)、财经新闻、消息推送、React前端
- PE/PB历史分位：接tushare token(~200元)补全
- 连板长期：多次--backfill-limit断点续传慢慢补
- TUI多屏(个股/板块情绪详情屏)

## 9. 数据复用
全部在 `data/stockfu.db`(SQLite单文件)。搬迁=拷这一个文件。已备份 `data/stockfu.db.bak.*`。日常--fetch增量,历史只增不减。
