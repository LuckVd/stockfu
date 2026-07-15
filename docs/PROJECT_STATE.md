# stockfu 项目状态（工作日志 / 冷启动手册）

> 新会话先读这份，就能接上。项目：`/opt/pro/stockfu/`。
> 定位：**StockFu·资产管理终端**，借鉴 `../daily_stock_analysis` 的多数据源 fallback 思想，TUI 为主 + FastAPI。

## 1. 一句话现状
一个本地优先的综合资产管理 + 市场情绪终端：持仓管理、股息/网格、**三层(市场/板块/个股) fear/greed/heat 情绪指数**、历史回补、**AI 4 顾问**、**天级回测引擎（算子→策略→逐日执行，未来函数已防护；2026-07-15 做减法：砍回测 LLM 算子 + 铲 ±20/signal → 纯连续因子）**、**因子诊断层（阶段2：单算子 IC/分位收益/换手/衰减，alphalens 思路）**、**时点宇宙+可成交（cn_large_pool_v1：list_date/ST/停牌 + 涨跌停/滑点；is_st/trade_status 入库闭环 + status_coverage；面向 ~800 大盘候选）**、**个股 PE/PB/PS 历史分位（baostock 全字段）**。SQLite 存储，textual TUI + FastAPI。

## 2. 架构（5 层）
```
stockfu/
├── main.py                    # 统一入口(--init-db/--buy/--backfill/--fetch/--serve/默认TUI)
├── stockfu/
│   ├── config.py              # pydantic-settings + setup_network(代理自动化)
│   ├── db.py                  # SQLModel engine + _migrate(开发期迁移) + seed
│   ├── models.py              # Asset/Transaction/Holding/DividendEvent/StockBasic
│   │                          #   /QuoteSnapshot(个股,含pe/pb/ps_ttm/pcf) /EtfQuoteDaily /IndexQuoteDaily
│   │                          #   /IndexSnapshot(三层scope) /MarketFactorDaily /SectorSnapshot(板块K线)
│   │                          #   /SectorFlowSnapshot(板块资金流) /StockExtDaily(个股资金流/两融) /NewsItem
│   ├── data/                  # 数据层(7源 fallback, 借鉴 daily_stock_analysis)
│   │   ├── base.py            # 市场识别/代码标准化/熔断器/TTL缓存/统一dataclass
│   │   ├── dividend_parser.py # 分红解析("派息"列/10, TTM)
│   │   ├── efinance_source.py # A股行情主力+日K(需传beg拉长历史)
│   │   ├── baostock_source.py # A股日K全字段(peTTM/pbMRQ/换手率)+PE/PB分位(免费无token)
│   │   ├── akshare_source.py  # A股分红+资金流+板块(同花顺K线/即时资金流/大盘资金流)+实时兜底
│   │   ├── tencent/sina/pytdx_source.py # 行情/K线 backup源(多源互为fallback)
│   │   ├── yfinance_source.py # 港美股行情+dividends+K线(period按days)
│   │   └── manager.py         # DataProviderManager 7源fallback
│   ├── services/
│   │   ├── factors.py         # 历史分位(估值类10年/情绪类5年窗口)
│   │   ├── valuation.py       # 个股PE/PB历史分位(读QuoteSnapshot<=asof,无未来函数)
│   │   ├── market_data.py     # 宏观因子(涨跌家数/连板/两融/北向/ERP/股东人数)
│   │   ├── composite.py       # 三层 fear/greed/heat 合成(多因子分位等权)+SECTOR_MAP/SECTOR_THS_NAME+ext_pcts
│   │   ├── indices.py         # 旧单因子fear/heat(保留fallback)
│   │   ├── portfolio.py       # 持仓汇总(市值/盈亏/股息率/年红利/回本)
│   │   ├── dividend.py        # 分红落库
│   │   ├── grid.py            # 股息率网格买卖计划
│   │   ├── fundflow.py        # ETF成交额活跃度(份额免费源无,用成交额代理)
│   │   ├── sentiment.py       # 板块情绪温度
│   │   ├── trading.py         # 交易录入(移动加权平均)
│   │   └── backfill.py        # 历史回补(两融总量/连板/个股两融/股息率序列/板块K线/大盘资金流)
│   ├── scheduler/jobs.py      # run_daily_job(行情+分红+ETF+三层指数) + backfill_kline + ensure_stock_data_and_index(加个股即算) + schedule
│   ├── ai/                    # 实盘AI 4顾问(docs/AI_ADVISORS.md,skills/) + operators算子平台(10:8math+2聚合,回测LLM已下线) + rebalancers选股层(3)
│   ├── backtest/              # 回测引擎(见 docs/BACKTEST.md): VirtualAccount+T+1+真实费用+完整metrics, 四层架构(算子→策略→rebalancer→执行)
│   ├── api/{server,routes}.py # FastAPI
│   └── tui/{app,trade_screen}.py  # textual 看板 + 交易录入模态屏
└── data/stockfu.db               # SQLite(全部历史,单文件可搬迁)
```

## 3. 已完成功能
- **资产管理**：持仓(移动加权平均)、TTM股息率、股息率网格买卖、收益里程碑(年红利/回本年限)
- **三层情绪指数**：fear/greed/heat × 市场/板块/个股。CNN式：多因子→历史分位(0-100)→等权平均
- **历史回补**：K线5年/两融总量11年/股息率5年序列/个股两融10天/连板(东财限流)
- **板块资金流**：板块K线+成交额(同花顺4年,sector_snapshot)/板块当日主力净流入(sector_flow_snapshot每日攒)/大盘资金流10factor(factor_snapshot)；compute_sector接入板块成交额分位(heat)+资金流(greed/fear)
- **TUI**：持仓看板(含个股恐慌/贪婪/热度三列) + 顶部市场fear/greed/heat(分档着色) + 按b/s交易录入；加个股自动后台补历史K线+算该股三层情绪指数
- **API**：/portfolio /quote /dividend /grid /indices/{market,sector,stock,history} /fundflow /sentiment
- **AI 顾问**：4 常驻顾问(趋势/逆向/风险/估值)+规则汇总+LLM润色，详见 `docs/AI_ADVISORS.md`
- **回测引擎(四层架构)**：算子(8math,纯连续score不clamp)→策略(5)→rebalancer选股(top_n/cap_rank/pass_through)→T+1执行。算子结果全局缓存(operator_result,指纹含源码hash自动失效;跨回测复用)+真实费用(佣/印/过)+完整metrics(夏普/胜率/超额)。CLI `--backtest` 走 scheduler.run；详见 `docs/BACKTEST.md`
- **回测性能优化(G09)**：operator meta 进程级 lru_cache + 删 4 冗余单列索引(复合唯一键覆盖热路径)+ WAL/synchronous=NORMAL/busy_timeout + `--vacuum` 维护工具。优化前后 metrics 逐值一致(防未来函数红线通过,详见 BACKTEST.md §6)
- **回测做减法(G10,2026-07-15)**：砍回测 LLM 算子(operators/llm/ 删 + hybrid/classic_4advisors 废弃 + active 默认→pure_factor)+ 铲 ±20/signal 体系(算子直出连续 score 不 clamp、signal 降级派生标签、统一 continuous 满仓锚点 score_full 参数化、OpResult 13→10 字段)+ 缓存指纹纳入源码 hash(治 P2-5)。**实盘 AI 4 顾问(ai/skills)不动**。确定性+防未来函数(前缀一致性)红线通过;属行为改变类(metrics 不与旧基准逐值一致,口径见 BACKTEST.md §8)。
- **因子诊断层(阶段2,2026-07-15)**：单算子连续 score 独立量化为 IC / 分位收益 / 换手 / 衰减(alphalens 思路),验证单个因子不必搭整条策略管道。新模块 `backtest/factor_diag.py`(纯 Python 统计,无 numpy/pandas)+ CLI `--factor-diag <operator>`(`--codes all`=全市场 801 票)。复用回测算子缓存(指纹逐字一致 → 跨场景互通);确定性 + 缓存复用 + 防未来函数已回归验证。详见 BACKTEST.md §11。
- **行情拆表**：QuoteSnapshot(个股,含pe/pb/ps_ttm/pcf/turnover) / EtfQuoteDaily(15只ETF,2021起) / IndexQuoteDaily(3指数,1990起) 三表分离,`quote_model_for` 按类型路由
- **个股估值分位**：baostock 全字段(peTTM/pbMRQ)backfill 落 QuoteSnapshot;`valuation_percentile(code,as_of,years=5)` 本地算 PE/PB 历史分位(无网络/无未来函数)。PE 覆盖792只/PB 800只(2021起约5年)
- **代理自动化**：setup_network(港美股走7890代理,国内源no_proxy直连)

## 4. 数据现状（关键）
| 因子 | 历史 | 说明 |
|------|------|------|
| 个股日K | 2020起(94.5万行/801只) | QuoteSnapshot;efinance主力,baostock/tencent/sina/pytdx backup;含pe/pb/ps_ttm/pcf |
| ETF日K | 2021起(1.6万行/15只) | EtfQuoteDaily(含510300);已拆表 |
| 指数日K | 1990起(1.4万行/3个) | IndexQuoteDaily;已拆表 |
| PE/PB/PS历史 | ✅有,2021起5年(PE792只/PB800只) | baostock全字段peTTM/pbMRQ落QuoteSnapshot;valuation.py算分位(原记❌无,已解决,无需tushare) |
| 两融总量 | 11年(2000条) | stock_margin_sse 一次拉全 |
| 股息率序列 | 5年(6083条) | 本地算=TTM分红/价格,受分红事件历史限制 |
| 个股两融 | 近10天 | stock_margin_detail_sse(date)按日筛code |
| 连板/涨停 | 近3周(限流) | stock_zt_pool_em(date),东财批量限流,断点续传 |
| 分红事件 | 2010起(344条/43只) | stock_history_dividend_detail "派息"列=每10股,/10 |
| 北向 | ❌无 | 2024起停服 |
| 涨跌家数 | ❌无 | legu不通/东财全量反爬 |
| 板块指数K线+成交额 | 4年(8132条) | 同花顺index_ths，绕东财限流；5行业板块 |
| 板块主力净流入 | 当日起攒(30条) | 同花顺fund_flow_industry即时；东财push2his历史源被限 |
| 宏观因子(MarketFactorDaily) | 2015起(2439条) | 涨跌家数/连板/两融/ERP等10+factor；东财限流时空 |
| 算子缓存(operator_result) | 565万行(跨回测复用) | 复合唯一键覆盖热路径,4单列索引已删;meta lru_cache+WAL+NORMAL,见 BACKTEST.md §6 |

## 5. 运行命令
```bash
cd /opt/pro/stockfu && source /opt/clash/proxy.sh
python main.py --init-db              # 建库+种子自选+演示持仓
python main.py --buy 600519 100 1500 --date 2024-01-15   # 录入持仓(移动加权)
python main.py --backfill 1825        # K线补5年
python main.py --backfill-factors     # 两融总量+个股两融10天+股息率序列
python main.py --backfill-limit 365   # 连板(限流分批,断点续传,多次跑)
python main.py --fetch                # 每日抓取+算三层指数落库
python main.py --backtest bollinger_monthly --start 2025-06-01 --end 2026-01-01 --codes 600519,000858 --save   # 回测(见 docs/BACKTEST.md)
python main.py --schedule             # APScheduler每日定时(工作日15:30抓行情/16:00发邮件)
python main.py                        # TUI看板
python main.py --serve                # API(127.0.0.1:8787)
python main.py --test-mail            # 立即出图发测试邮件(自包含,内嵌serve)
# 单进程 daemon(挂服务器):--schedule邮件启用时内嵌uvicorn,一条命令=web+调度+定时邮件
nohup python main.py --schedule >> data/schedule.log 2>&1 &
```

## 6. 关键设计决策
- **情绪指数公式**：多因子→各历史分位→等权平均(CNN式)。fear=下行因子,greed=上行,heat=活跃
- **历史窗口**：估值类(PE/PB/股息率)10年,情绪/量价类5年。当前K线已5年;PE/PB 仅5年(2021起),待 backfill 延至10年
- **三层粒度**：index_snapshot(level=market/sector/stock, scope=MARKET/板块名/code)
- **回测防未来函数**：取数一律带 `<= as_of` 上界(quote_series/IndexSnapshot)，信号用 T-1 数据、T+1 开盘执行；修复前 bollinger 虚高 +39.62%，堵漏后真实 -4.14%。详见 docs/BACKTEST.md
- **估值数据源**：PE/PB/PS_TTM/PCF 由 baostock 全字段日K落 QuoteSnapshot(2021起约5年,免费无token);分位由 valuation.py 本地算,无需 tushare
- **代理**：mihomo 7890(港股/美股yfinance必须);国内源(akshare/efinance)no_proxy直连
- **代理切节点**：9090 API无密码,主组🚀节点选择,新加坡最稳(见memory mihomo-proxy-switching)

## 7. 已知数据坑（调试要点）
- akshare 1.18：`stock_margin_account_sse`不存在→用`stock_margin_sse`;`stock_a_indicator_lg`不存在→PE/PB 改走 baostock(peTTM/pbMRQ,免费,已落 QuoteSnapshot)
- `stock_margin_detail_sse(date)`只接受date参数,返回全市场需筛code;今日数据常缺,往前找交易日
- 东财反爬：`stock_zh_a_spot_em`(全量行情)/`stock_sector_fund_flow_rank`(板块)时不稳;`stock_zt_pool_em`批量限流→连板只能分批补
- efinance：`get_realtime_quotes`已坏(报"行情参数不正确");`get_quote_history`**必须传beg**才拉长历史(默认只近期)
- yfinance：港股符号`0700.HK`(4位补零);A股`600519.SS`;需代理;period按days选
- A股分红：`stock_history_dividend_detail`"派息"列是**每10股**,每股=/10
- 招行股息率偏高(13%):数据源税前口径,需校准
- 板块历史源：东财push2his(sector/concept_fund_flow_hist/board_industry_hist_em)限流全空→改同花顺stock_board_industry_index_ths(板块K线4年,绕限流)+stock_fund_flow_industry(即时)
- 板块名映射：SECTOR_MAP键(医药/新能源车)对不上同花顺行业名(医疗服务/汽车整车)→composite.SECTOR_THS_NAME显式映射;宽基(沪深300等)无对应→compute_sector降级纯ETF
- 同花顺列名：OHLC带"价"后缀(开盘价vs东财开盘);资金流"净额"列排除"净占比","大单"排除"超大单"(akshare_source._find_col)
- pip：系统python PEP668,用`--break-system-packages`

## 8. 待办（P2 / 未来）

### 【下一步 · 优先】(阶段2 后规划,2026-07-15)
- **阶段2 · 因子诊断层 ✅ 已完成(2026-07-15)**：alphalens 思路——单算子连续 score 算 IC / 分位收益 / 换手 / 衰减。`backtest/factor_diag.py` + CLI `--factor-diag <operator>`,复用回测算子缓存。详见 BACKTEST.md §11。
- **阶段3 · 执行层抽象**(`docs/ARCHITECTURE_REVIEW.md` §4 P2 清单):Broker 抽象(回测/实盘共用执行层 P2-2)+ Sizer/CommInfo(费率/整手可替换 P2-3)+ Analyzer 可组合(_metrics 拆分 P2-4)+ Position 开平分解(交易回合统计 P2-8)+ math 算子向量化 run_batch(治冷启动慢 P2-1/P2-7)。建议顺序:P2-5(✅done in G10)→P2-1/7(性能)→P2-8/4(统计)→P2-3/2(执行层)。
- **G10 遗留 ✅ 已清理(2026-07-15)**:① `--vacuum` 已跑(1133MB→191MB,回收 ~942MB 空闲页;备份 `data/stockfu.db.bak.G09`);② `action.py compute_target_weight` docstring 重写(去乱码+已删的 mode/targets 提法);③ `raw_score` 死字段清理——`models.py` 删字段+`db.py _migrate` ADD→DROP COLUMN(SQLite 3.45,幂等,物理列已删),同步清理 OperatorResult 类 docstring 的 LLM/raw_score 残留描述。

### 【常规待办】
- **回测基准 G02 已激活**：基准 = 上证综指 sh000001(IndexQuoteDaily);_benchmark_curve 直读不走 quote_model_for;run_scheduled_fetch 每日更新;benchmark_return/excess/benchmark_window 恒定产出
- ~~classic_4advisors/hybrid~~ 已于 G10(2026-07-15)废弃(砍回测 LLM 算子);回测剩 5 个纯 math 策略(pure_factor/macd_cross/momentum_breakout/dual_bollinger/bollinger_reversion)
- 估值窗口：PE/PB 仅5年(2021起),backfill 延至10年匹配估值类分位窗口(baostock,无需tushare)
- 连板长期：多次--backfill-limit断点续传慢慢补
- 板块轮动信号：连续N日净流入排名/板块间资金切换(历史地基已就位)
- TUI多屏(个股/板块情绪详情屏)；美股 quote_snapshot 抓取修复(AAPL 等为空)

## 9. 数据复用
全部在 `data/stockfu.db`(SQLite单文件,**WAL 模式**)。搬迁=拷这一个文件——**WAL 下先 `PRAGMA wal_checkpoint(TRUNCATE)` 或一并拷 -wal/-shm**;`--vacuum` 回收空闲页。已备份 `data/stockfu.db.bak.*`。日常--fetch增量,历史只增不减。
