"""数据模型（SQLModel / SQLite）。

覆盖：资产、交易流水、聚合持仓、分红事件、行情天级快照、
指数快照(三层:市场/板块/个股)、因子快照(可追溯)、ETF 资金流快照、新闻。
"""
from datetime import date, datetime
from enum import Enum

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"
    DIVIDEND = "dividend"


def _now() -> datetime:
    return datetime.now()


class Asset(SQLModel, table=True):
    __tablename__ = "asset"
    code: str = Field(primary_key=True)           # 标准化代码 600519 / HK00700 / AAPL
    name: str = ""
    market: str = Field(default="cn", index=True)  # cn/hk/us/jp/kr/tw
    asset_type: str = Field(default="stock")       # stock/fund_etf/fund_otc/index/bond
    sector: str = ""                               # 板块/行业
    currency: str = "CNY"
    is_watch: bool = Field(default=False, index=True)
    updated_at: datetime = Field(default_factory=_now)


class Transaction(SQLModel, table=True):
    __tablename__ = "transaction"
    id: int | None = Field(default=None, primary_key=True)
    asset_code: str = Field(foreign_key="asset.code", index=True)
    side: str = Field(default=Side.BUY.value)
    shares: float = 0.0
    price: float = 0.0
    amount: float = 0.0
    fee: float = 0.0
    trade_date: date = Field(default_factory=date.today, index=True)
    note: str = ""


class Holding(SQLModel, table=True):
    """聚合持仓（可由 transactions 汇总，也可直接录入）。"""
    __tablename__ = "holding"
    asset_code: str = Field(foreign_key="asset.code", primary_key=True)
    shares: float = 0.0
    avg_cost: float = 0.0
    total_cost: float = 0.0
    first_buy_date: date | None = None
    updated_at: datetime = Field(default_factory=_now)


class DividendEvent(SQLModel, table=True):
    __tablename__ = "dividend_event"
    id: int | None = Field(default=None, primary_key=True)
    asset_code: str = Field(foreign_key="asset.code", index=True)
    ex_date: date = Field(index=True)
    record_date: date | None = None
    announce_date: date | None = None
    per_share_cash: float = 0.0
    # 每旧股新增股数(送股+转增)。0=纯现金事件；保留在同一除权事件中。
    per_share_stock: float = 0.0
    # 派息日 / 红股上市日(baostock payDate/stockMktDate)。研究模式落库供诊断;
    # non-strict 主线收益用 qfq(已含分红),这两日仅作交叉校验/展示。
    pay_date: date | None = None
    stock_mkt_date: date | None = None
    # baostock dividCashPsAfterTax(税后每股股利)。源端固定扣税近似、非持有期分档;
    # 研究模式仅落库供诊断,不据此宣称税后精确(见 docs/BACKTEST.md §0.3/§0.6)。
    per_share_cash_after_tax: float | None = None
    currency: str = "CNY"
    source: str = ""


class LhbEvent(SQLModel, table=True):
    """龙虎榜上榜事件(东财每日明细,2026-08 接入判别后落库)。

    PIT 约定:榜单盘后披露,``lhb_date`` 当日收盘后可见、T+1 可交易(引擎天然满足)。
    唯一键 (asset_code, lhb_date, reason)——同一票同一日可因多个原因各记一条。
    ``inst_buy_count/inst_sell_count`` 从东财"解读"文本解析的机构家数(买入/卖出),
    0=无机构参与;``success_rate`` 为东财给出的机构历史成功率(部分事件缺失)。
    """

    __tablename__ = "lhb_event"
    id: int | None = Field(default=None, primary_key=True)
    asset_code: str = Field(index=True)
    lhb_date: date = Field(index=True)
    reason: str = ""
    buy_amount: float | None = None        # 龙虎榜买入额(元)
    sell_amount: float | None = None       # 龙虎榜卖出额(元)
    net_amount: float | None = None        # 龙虎榜净买额(元)
    net_ratio: float | None = None         # 净买额占总成交比(%)
    close: float | None = None             # 上榜日收盘价
    pct_chg: float | None = None           # 上榜日涨跌幅(%)
    turnover: float | None = None          # 上榜日换手率(%)
    float_mktcap: float | None = None      # 流通市值(元)
    inst_buy_count: int = 0
    inst_sell_count: int = 0
    success_rate: float | None = None      # 解读中机构历史成功率(%)
    source: str = "akshare"


class BackfillCheckpoint(SQLModel, table=True):
    """逐项网络回补的持久化进度。

    ``task_key + scope_key + item_key`` 唯一标识一次可复用的成功结果。scope_key
    必须包含数据范围和实现版本；范围或解析口径改变时自然形成新任务，而不是把
    旧的“成功”误当作新任务已完成。
    """
    __tablename__ = "backfill_checkpoint"
    id: int | None = Field(default=None, primary_key=True)
    task_key: str = Field(index=True)
    scope_key: str = Field(index=True)
    item_key: str = Field(index=True)
    status: str = Field(default="failed", index=True)  # success / failed
    attempts: int = 0
    last_error: str = ""
    updated_at: datetime = Field(default_factory=_now)
    __table_args__ = (UniqueConstraint(
        "task_key", "scope_key", "item_key", name="uq_backfill_checkpoint_item",
    ),)


# 方案A 账本表(corporate_action_source_record/event)随 2026-07-27 研究模式反转移除。
# 研究模式直接用 dividend_event(baostock 落库),不自建多源仲裁账本(见 docs/BACKTEST.md §0)。


# ---- 财务三表 PIT 快照（东财 datacenter-web，2026-08） ----
# 按报告期一次拉全市场；每股票每报告期一行；pub_date=NOTICE_DATE 公告日（PIT
# 时点过滤的唯一依据），stat_date=报告期。字段名与东财原始 JSON 对应（snake_case）。
# 关联键 (asset_code, year, quarter)；asset_code 与 quote_snapshot 等全库统一（6 位无前缀）。
# 设计文档：docs/SPECS/financial-data-design.md；回补脚本：services/backfill_financial.py。
# financial_operation / financial_dupont 因东财无按报告期接口，已决定不预留（见设计文档 §5）。


class FinancialBase(SQLModel):
    """财务表公共字段（非 table 基类，子类各自建表）。"""
    id: int | None = Field(default=None, primary_key=True)
    asset_code: str = Field(index=True)
    year: int = Field(index=True)
    quarter: int = Field(index=True)          # 1-4（4=年报）
    pub_date: date | None = Field(default=None, index=True)   # 公告日 NOTICE_DATE（PIT 关键）
    stat_date: date | None = None             # 报告期（如 2024-03-31）
    source: str = "eastmoney"
    updated_at: datetime = Field(default_factory=_now)


class FinancialProfit(FinancialBase, table=True):
    """业绩报表（RPT_LICO_FN_CPD）：ROE/毛利率/归母净利/营收/同比/EPS。"""
    __tablename__ = "financial_profit"
    roe_avg: float | None = None              # 净资产收益率 WEIGHTAVG_ROE
    gp_margin: float | None = None            # 销售毛利率 XSMLL
    net_profit: float | None = None           # 归母净利润 PARENT_NETPROFIT
    eps: float | None = None                  # 基本每股收益 BASIC_EPS
    revenue: float | None = None              # 营业总收入 TOTAL_OPERATE_INCOME
    revenue_yoy: float | None = None          # 营收同比(%) YSTZ
    net_profit_yoy: float | None = None       # 净利同比(%) SJLTZ
    bps: float | None = None                  # 每股净资产 BPS
    cash_per_share: float | None = None       # 每股经营现金流 MGJYXJJE
    __table_args__ = (UniqueConstraint("asset_code", "year", "quarter",
                                       name="uq_financial_profit_code_yq"),)


class FinancialGrowth(FinancialBase, table=True):
    """成长能力：净利同比/总资产同比/股东权益同比（部分字段暂留空，见设计文档 §5）。"""
    __tablename__ = "financial_growth"
    yoy_ni: float | None = None               # 净利同比(%) SJLTZ
    yoy_asset: float | None = None            # 总资产同比(%) TOTAL_ASSETS_YOY
    yoy_equity: float | None = None           # 股东权益同比(%)（自算，暂空）
    yoy_eps_basic: float | None = None        # 每股收益同比(%)（暂无源，留空）
    yoy_pni: float | None = None              # 利润总额同比(%)（暂无源，留空）
    __table_args__ = (UniqueConstraint("asset_code", "year", "quarter",
                                       name="uq_financial_growth_code_yq"),)


class FinancialBalance(FinancialBase, table=True):
    """资产负债表（RPT_DMSK_FN_BALANCE）：总资产/总负债/资产负债率/股东权益/货币资金/应收/存货。"""
    __tablename__ = "financial_balance"
    total_assets: float | None = None         # 总资产 TOTAL_ASSETS
    total_liabilities: float | None = None    # 总负债 TOTAL_LIABILITIES
    liability_to_asset: float | None = None   # 资产负债率(%) LIABILITY_TO_ASSET
    equity: float | None = None               # 股东权益合计 TOTAL_EQUITY
    monetary_fund: float | None = None        # 货币资金 MONETARYFUNDS
    receivables: float | None = None          # 应收账款 ACCOUNTS_RECE
    inventory: float | None = None            # 存货 INVENTORY
    payable: float | None = None              # 应付账款 ACCOUNTS_PAYABLE
    current_ratio: float | None = None        # 流动比率 CURRENT_RATIO
    total_assets_yoy: float | None = None     # 总资产同比(%)（东财无直接源，自算或留空）
    __table_args__ = (UniqueConstraint("asset_code", "year", "quarter",
                                       name="uq_financial_balance_code_yq"),)


class FinancialCashflow(FinancialBase, table=True):
    """现金流量表（RPT_DMSK_FN_CASHFLOW）：经营/投资/融资现金流净额。"""
    __tablename__ = "financial_cashflow"
    net_cash_oper: float | None = None        # 经营现金流净额 NETCASH_OPERATE
    net_cash_inv: float | None = None         # 投资现金流净额 NETCASH_INVEST
    net_cash_fin: float | None = None         # 融资现金流净额 NETCASH_FINANCE
    net_cash_total: float | None = None       # 现金净增加额 NETCASH_TOTAL
    __table_args__ = (UniqueConstraint("asset_code", "year", "quarter",
                                       name="uq_financial_cashflow_code_yq"),)


class QuoteSnapshot(SQLModel, table=True):
    """个股日行情快照。价格分三套复权口径(baostock adjustflag):

    - *_qfq / 遗留 open·high·low·close: **前复权**(flag=2)。回测成交、动量/低波等价量因子默认用它。
      遗留四列 ≡ qfq,写入时同步,兼容旧 SQL/ETF 风格调用。
    - *_raw: **不复权**(flag=3)。股息率等「名义现金/股价」分母必须用它(防 qfq 前视)。
    - *_hfq: **后复权**(flag=1)。备用(长周期绝对价对比等)。

    volume/amount/pe/pb/状态列与复权无关,共享一行。
    """
    __tablename__ = "quote_snapshot"
    id: int | None = Field(default=None, primary_key=True)
    asset_code: str = Field(foreign_key="asset.code", index=True)
    quote_date: date = Field(default_factory=date.today, index=True)
    # ── 前复权(遗留别名,≡ *_qfq) ──
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float = 0.0
    # ── 前复权(显式) ──
    open_qfq: float | None = None
    high_qfq: float | None = None
    low_qfq: float | None = None
    close_qfq: float | None = None
    # ── 不复权 ──
    open_raw: float | None = None
    high_raw: float | None = None
    low_raw: float | None = None
    close_raw: float | None = None
    # ── 后复权 ──
    open_hfq: float | None = None
    high_hfq: float | None = None
    low_hfq: float | None = None
    close_hfq: float | None = None
    pct_chg: float | None = None
    volume: float | None = None
    amount: float | None = None
    turnover: float | None = None                  # 换手率 %
    pe: float | None = None
    pb: float | None = None
    market_cap: float | None = None
    # 日状态(baostock 全字段回补已落库;ORM 对齐物理列,供宇宙/可成交)
    trade_status: int | None = None               # 1=交易 0=停牌
    is_st: int | None = None                      # 1=ST/*ST 0=正常
    __table_args__ = (UniqueConstraint("asset_code", "quote_date", name="uq_quote_code_date"),)


class SecurityMaster(SQLModel, table=True):
    """A 股证券主数据(时点宇宙用):上市/退市日 + 板块(涨跌幅档)。

    与 asset 表解耦——回测池 ~800 只不必全部进自选。list_date 防次新股名单污染。
    """
    __tablename__ = "security_master"
    code: str = Field(primary_key=True)            # 标准化 600519
    name: str = ""
    list_date: date | None = None                  # 上市日
    delist_date: date | None = None                # 退市日(空=在市)
    board: str = "main"                            # main/chinext/star/bse → 10/20/20/30%
    status: str = "1"                              # baostock status 1=上市 0=退市
    updated_at: datetime = Field(default_factory=_now)


class IndexConstituent(SQLModel, table=True):
    """指数历史成分的有效区间；右边界为开区间。"""
    __tablename__ = "index_constituent"
    id: int | None = Field(default=None, primary_key=True)
    index_code: str = Field(index=True)
    asset_code: str = Field(index=True)
    effective_from: date = Field(index=True)
    effective_to: date | None = Field(default=None, index=True)
    announce_date: date | None = Field(default=None, index=True)
    source: str = ""
    source_ref: str = ""
    imported_at: datetime = Field(default_factory=_now)
    __table_args__ = (UniqueConstraint(
        "index_code", "asset_code", "effective_from",
        name="uq_index_constituent_code_from"),)


class IndexSnapshot(SQLModel, table=True):
    """情绪指数天级快照，三层粒度：market / sector / stock。

    scope: market→"MARKET"；sector→板块名；stock→股票 code。
    components: JSON，记录各因子的分位明细（可追溯）。
    """
    __tablename__ = "index_snapshot"
    id: int | None = Field(default=None, primary_key=True)
    index_key: str = Field(index=True)             # fear / greed / heat
    level: str = Field(default="market", index=True)
    scope: str = Field(default="MARKET", index=True)
    snap_date: date = Field(default_factory=date.today, index=True)
    value: float = 0.0
    components: str = ""                           # JSON 因子分位明细
    note: str = ""
    __table_args__ = (UniqueConstraint(
        "index_key", "level", "scope", "snap_date", name="uq_idx_scope_date"),)


class FactorSnapshot(SQLModel, table=True):
    """单个因子的天级快照（原始值 + 历史分位 + 窗口），便于追溯和重算权重。"""
    __tablename__ = "factor_snapshot"
    id: int | None = Field(default=None, primary_key=True)
    level: str = Field(index=True)                 # market/sector/stock
    scope: str = Field(index=True)
    factor: str = Field(index=True)                # volatility/turnover/pe/rs/...
    snap_date: date = Field(default_factory=date.today, index=True)
    raw_value: float | None = None
    percentile: float | None = None                # 0-100
    window_days: int = 250
    sample_size: int = 0
    __table_args__ = (UniqueConstraint(
        "level", "scope", "factor", "snap_date", name="uq_factor_scope_date"),)


class IndexQuoteDaily(SQLModel, table=True):
    """指数日线行情（上证综指 etc.），供回测基准使用。
    
    表已存在（G01 遗存），仅作 ORM 映射，不重建。
    """
    __tablename__ = "index_quote_daily"
    id: int | None = Field(default=None, primary_key=True)
    asset_code: str = Field(index=True)
    quote_date: date = Field(index=True)
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float = 0.0
    pct_chg: float | None = None
    volume: float | None = None
    amount: float | None = None
    __table_args__ = (UniqueConstraint("asset_code", "quote_date", name="uq_index_quote_code_date"),)


class EtfQuoteDaily(SQLModel, table=True):
    """ETF 日线行情(行业 ETF etc.);表已存在,仅作 ORM 映射,不重建。
    schema 同 IndexQuoteDaily,供回测/探测按代码路由读取(指数走 index 表,ETF 走本表)。"""
    __tablename__ = "etf_quote_daily"
    id: int | None = Field(default=None, primary_key=True)
    asset_code: str = Field(index=True)
    quote_date: date = Field(index=True)
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    pct_chg: float | None = None
    volume: float | None = None
    amount: float | None = None
    __table_args__ = (UniqueConstraint("asset_code", "quote_date", name="uq_etf_quote_code_date"),)


class FundFlowSnapshot(SQLModel, table=True):
    """ETF 份额资金流向天级快照（份额变化 ≈ 大资金净申赎）。"""
    __tablename__ = "fundflow_snapshot"
    id: int | None = Field(default=None, primary_key=True)
    etf_code: str = Field(index=True)
    snap_date: date = Field(default_factory=date.today, index=True)
    shares_outstanding: float | None = None        # 份额（亿份）
    nav: float | None = None                       # 净值/价格
    net_inflow: float | None = None                # 估算净流入（亿元）
    __table_args__ = (UniqueConstraint("etf_code", "snap_date", name="uq_fundflow_code_date"),)


class SectorSnapshot(SQLModel, table=True):
    """板块指数K线天级快照（同花顺 stock_board_industry_index_ths，4年历史）。

    板块自身的 OHLC + 成交额，支撑板块热度/走势的历史分位（区别于代表 ETF 的 quote_snapshot）。
    sector_name 取自 composite.SECTOR_MAP 的键（中文板块名）。
    """
    __tablename__ = "sector_snapshot"
    id: int | None = Field(default=None, primary_key=True)
    sector_name: str = Field(index=True)
    snap_date: date = Field(default_factory=date.today, index=True)
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    pct_chg: float | None = None
    volume: float | None = None
    amount: float | None = None                  # 板块成交额（heat 关键因子）
    __table_args__ = (UniqueConstraint("sector_name", "snap_date", name="uq_sector_name_date"),)


class SectorFlowSnapshot(SQLModel, table=True):
    """板块当日主力资金流天级快照（同花顺 stock_fund_flow_industry 即时，每日 --fetch 攒历史）。

    东财 push2his 历史源限流不稳，故净流入靠每日即时落库累积（首日无分位，越跑越准）。
    """
    __tablename__ = "sector_flow_snapshot"
    id: int | None = Field(default=None, primary_key=True)
    sector_name: str = Field(index=True)
    snap_date: date = Field(default_factory=date.today, index=True)
    net_inflow: float | None = None              # 主力净额
    net_inflow_pct: float | None = None          # 主力净流入占比（历史源可提供）
    inflow: float | None = None
    outflow: float | None = None
    company_count: int | None = None
    leading_stock: str = ""
    leading_chg: float | None = None
    index_pct_chg: float | None = None           # 行业指数涨跌幅
    __table_args__ = (UniqueConstraint("sector_name", "snap_date", name="uq_sector_flow_name_date"),)


class NewsItem(SQLModel, table=True):
    __tablename__ = "news_item"
    id: int | None = Field(default=None, primary_key=True)
    title: str
    summary: str = ""
    url: str = ""
    source: str = ""
    published_at: datetime | None = None
    related_code: str = Field(default="", index=True)
    sentiment: float | None = None                 # -1..1


class AppConfig(SQLModel, table=True):
    """通用键值配置（运行时可变设置，如外网代理地址）。"""
    __tablename__ = "app_config"
    key: str = Field(primary_key=True)
    value: str = ""
    updated_at: datetime = Field(default_factory=_now)


class Strategy(SQLModel, table=True):
    """回测策略配置（YAML格式存储）。"""
    __tablename__ = "strategy"
    strategy_id: str = Field(primary_key=True)
    name: str = ""
    config: str = ""  # YAML配置
    note: str = ""
    updated_at: datetime = Field(default_factory=_now)


class Operator(SQLModel, table=True):
    """算子定义（Math算子或LLM顾问）。"""
    __tablename__ = "operator"
    operator_id: str = Field(primary_key=True)
    name: str = ""
    type: str = ""  # llm 或 math
    module: str = ""  # 模块路径
    params_schema: str = ""  # 参数schema JSON
    prompt: str = ""
    constitution_ref: str = ""
    display_order: int = 0
    active: bool = True
    version: int = 0
    updated_at: datetime = Field(default_factory=_now)


class OperatorResult(SQLModel, table=True):
    """算子级回测缓存:单算子在 (code,as_of,fingerprint) 下的 OpResult。

    去持仓依赖后 math 算子是纯市场数据函数 → 同输入全局任意复用(跨策略/跨回测/跨因子诊断)。
    aggregator 不缓存(纯函数重算廉价)。fingerprint=hash(version+params+source);改算子源码
    自动失效(治 P2-5)。score 连续不 clamp(G10 铲除 ±20 后原 raw_score 已并入)。
    核心列提独立列便于 SQL 查询/统计;math 行 detail=NULL。复合唯一键 uq_op_result_code_date_op_fp
    覆盖全部热路径查询,四个单列索引已删(见 db._migrate)。
    """
    __tablename__ = "operator_result"
    id: int | None = Field(default=None, primary_key=True)
    asset_code: str = Field()
    as_of: date = Field()
    operator_id: str = Field()                       # momentum / trend / ...
    operator_type: str = Field(default="math")       # math | llm(aggregator 不入库)
    fingerprint: str = Field()                       # hash(version+params+source);改算子源码→自动失效
    # 核心列(可 SQL 查询/统计)
    signal: str | None = None                        # 派生标签(展示用,不参与决策)
    score: float | None = None                       # 连续强度(不 clamp;原 raw_score 已并入)
    confidence: float | None = None
    veto: bool = False
    target_weight: float | None = None
    value: float | None = None                       # math 算子原始值(供 ctx.factors 共享)
    detail: str | None = None                        # JSON: {reasoning,evidence,tools_used};math=NULL,LLM 存
    updated_at: str | None = None                    # "YYYY-MM-DD HH:MM"
    __table_args__ = (UniqueConstraint("asset_code", "as_of", "operator_id", "fingerprint",
                                       name="uq_op_result_code_date_op_fp"),)
