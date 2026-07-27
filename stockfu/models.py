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
    currency: str = "CNY"
    source: str = ""


class CorporateActionSourceRecord(SQLModel, table=True):
    """公司行为的不可变来源记录。

    该表是回灌的落点，不供策略或账户直接读取。相同供应商记录用
    ``(source, source_event_key)`` 幂等，供应商修订则必须使用新的 event key 或
    content hash；绝不覆盖已保存的原始证据。
    """
    __tablename__ = "corporate_action_source_record"
    id: int | None = Field(default=None, primary_key=True)
    asset_code: str = Field(foreign_key="asset.code", index=True)
    source: str = Field(index=True)               # baostock / exchange / vendor:...
    source_event_key: str = Field(index=True)     # 源端稳定键；无键时由抓取器构造
    source_revision: str = ""
    action_type: str = "distribution"            # distribution / rights / merger / delisting
    ex_date: date = Field(index=True)
    record_date: date | None = None
    announce_date: date | None = None
    pay_date: date | None = None
    stock_mkt_date: date | None = None
    per_share_cash: float = 0.0
    per_share_stock: float = 0.0
    rights_ratio: float = 0.0
    rights_price: float | None = None
    terminal_price: float | None = None
    currency: str = "CNY"
    payload_sha256: str = ""
    raw_payload: str = ""                         # 原响应的规范 JSON；保留审计证据
    ingested_at: datetime = Field(default_factory=_now, index=True)
    __table_args__ = (UniqueConstraint(
        "source", "source_event_key", name="uq_corporate_action_source_key"),)


class CorporateActionEvent(SQLModel, table=True):
    """仲裁后的正式公司行为事件；按 revision append-only 保存。"""
    __tablename__ = "corporate_action_event"
    id: int | None = Field(default=None, primary_key=True)
    action_id: str = Field(index=True)             # 稳定逻辑键，如 600519:2018-06-15:distribution
    revision: int = 1
    asset_code: str = Field(foreign_key="asset.code", index=True)
    action_type: str = "distribution"
    ex_date: date = Field(index=True)
    record_date: date | None = None
    announce_date: date | None = None
    pay_date: date | None = None
    stock_mkt_date: date | None = None
    per_share_cash: float = 0.0
    per_share_stock: float = 0.0
    rights_ratio: float = 0.0
    rights_price: float | None = None
    terminal_price: float | None = None
    currency: str = "CNY"
    status: str = Field(default="needs_review", index=True)  # accepted / rejected / superseded
    source_record_ids: str = "[]"                  # JSON，来源记录主键列表
    decision_note: str = ""
    supersedes_event_id: int | None = Field(default=None, foreign_key="corporate_action_event.id")
    created_at: datetime = Field(default_factory=_now, index=True)
    __table_args__ = (UniqueConstraint(
        "action_id", "revision", name="uq_corporate_action_revision"),)


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
