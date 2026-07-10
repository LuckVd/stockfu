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
    note: str = ""
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
    currency: str = "CNY"
    source: str = ""


class QuoteSnapshot(SQLModel, table=True):
    """行情天级快照（历史落库，支撑历史分析 / 指数 / 因子分位）。"""
    __tablename__ = "quote_snapshot"
    id: int | None = Field(default=None, primary_key=True)
    asset_code: str = Field(foreign_key="asset.code", index=True)
    quote_date: date = Field(default_factory=date.today, index=True)
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float = 0.0
    pct_chg: float | None = None
    volume: float | None = None
    amount: float | None = None
    turnover: float | None = None                  # 换手率 %
    pe: float | None = None
    pb: float | None = None
    market_cap: float | None = None
    __table_args__ = (UniqueConstraint("asset_code", "quote_date", name="uq_quote_code_date"),)


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

    去持仓依赖后所有算子(math/llm)纯市场数据 → 同输入全局任意复用(跨策略/跨回测)。
    aggregator 不缓存(纯函数重算廉价)。fingerprint:math=hash(params)/llm=hash(prompt+temp)。
    核心列提独立列便于 SQL 查询/统计;reasoning/evidence/tools_used 进 detail JSON(回放)。
    """
    __tablename__ = "operator_result"
    id: int | None = Field(default=None, primary_key=True)
    asset_code: str = Field(index=True)
    as_of: date = Field(index=True)
    operator_id: str = Field(index=True)            # momentum / trend / ...
    operator_type: str = Field(default="math")      # math | llm(aggregator 不入库)
    fingerprint: str = Field(index=True)            # 输入摘要(16位 sha1);prompt 改→自动失效
    # 核心列(可 SQL 查询/统计)
    signal: str | None = None                       # strong_buy/buy/hold/sell/strong_sell
    score: float | None = None
    confidence: float | None = None
    veto: bool = False
    target_weight: float | None = None
    value: float | None = None                      # math 算子原始值(供 ctx.factors 共享)
    detail: str | None = None                       # JSON: {reasoning,evidence,tools_used}(回放)
    updated_at: str | None = None                   # "YYYY-MM-DD HH:MM"
    __table_args__ = (UniqueConstraint("asset_code", "as_of", "operator_id", "fingerprint",
                                       name="uq_op_result_code_date_op_fp"),)
