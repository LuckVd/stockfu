// 后端 API 返回类型（照 routes.py + services 层字段定义）。

/** GET /portfolio 持仓项 */
export interface PositionView {
  code: string
  name: string
  market: string
  currency: string          // CNY / USD / HKD
  shares: number
  avg_cost: number
  price: number
  market_value: number
  cost: number
  profit: number
  profit_pct: number
  ttm_yield_pct: number | null
  annual_dividend: number
  recovered_pct: number
  payback_years: number | null
  fear: number | null
  greed: number | null
  heat: number | null
  day_chg: number | null
}

/** GET /portfolio */
export interface PortfolioResp {
  positions: PositionView[]
  total_cost: number
  total_value: number
  total_profit: number
  blended_yield_pct: number
  annual_dividend_income: number
  as_of: string
  mixed_currency: boolean
}

/** GET /watchlist 自选项 */
export interface WatchlistItem {
  code: string
  name: string
  market: string
  currency: string
  type: string              // stock / etf / fund
  price: number | null
  day_chg: number | null
  ttm_yield_pct: number | null
  fear: number | null
  greed: number | null
  heat: number | null
  is_holding: boolean
}

/** GET /sectors/flow 单板块 */
export interface FlowItem {
  name: string
  net_inflow: number        // 亿元
}

/** GET /sectors/flow?top_n= */
export interface FlowResp {
  count: number
  top: FlowItem[]
  bottom: FlowItem[]
}

/** GET /indices/stock/{code} 个股情绪详情 */
export interface StockIndexResp {
  level: string
  scope: string
  fear: number | null
  greed: number | null
  heat: number | null
  today_chg: number | null
  components: {
    volatility_pct?: number | null
    momentum_pct?: number | null
    amount_pct?: number | null
    pe_pct?: number | null
    pb_pct?: number | null
    [k: string]: number | null | undefined
  }
  factor_counts: { fear: number; greed: number; heat: number }
}

/** DELETE /holding/{code} */
export interface DelHoldingResp {
  ok: true
  code: string
  deleted_transactions: number
}

/** DELETE /watch/{code} */
export interface DelWatchResp {
  ok: true
  code: string
  is_watch: false
}

/** POST /trade */
export interface TradeResp {
  shares: number
  avg_cost: number
  total_cost: number
}

/** POST /watch/{code} */
export interface WatchAddResp {
  ok: true
  code: string
  is_watch: true
}

/** POST /stock/{code}/ensure */
export interface EnsureResp {
  ok: true
  code: string
  status: string
  detail?: string
}

/* ----- Phase 3 config / csv ----- */

/** GET /config/proxy */
export interface ProxyConfig {
  proxy_url: string
  source: 'db' | 'env'
  effective: string
}
/** POST /config/proxy/test（三态：200 ok / 403·429 IP封 / null 连不上）*/
export interface ProxyTestResult {
  ok: boolean
  status?: number | null
  latency_ms: number | null
  detail: string
}
/** GET /config/schedule */
export interface ScheduleConfig {
  daily_fetch_time: string
  fetch_retry_interval: number
  fetch_retry_count: number
  source: 'db' | 'env'
}
/** GET /config/mail 单个邮箱预设 */
export interface MailPreset {
  host: string
  port: number
  ssl: boolean
  label: string
}
/** GET /config/mail */
export interface MailConfig {
  smtp_host: string
  smtp_port: number
  smtp_user: string
  has_password: boolean
  smtp_from: string
  mail_to: string
  mail_enabled: boolean
  mail_time: string
  mail_days: string
  presets: Record<string, MailPreset>
}
/** POST /config/mail/test（多态失败：未配置/出图失败/无图/SMTP失败/成功）*/
export interface MailTestResult {
  ok: boolean
  detail: string
  pages?: number
  to?: string[]
  subject?: string
}
/** GET /config/llm */
export interface LlmConfig {
  llm_base_url: string
  llm_model: string
  has_api_key: boolean
  source: 'db' | 'env'
}

/** POST /config/llm/test */
export interface LlmTestResult {
  ok: boolean
  detail: string
  reply?: string
}
/** POST /csv/import/{scope} */
export interface CsvImportResult {
  ok: true
  scope: string
  table: string
  counts: { inserted: number; updated: number; skipped: number }
  bg_ensure: string[]
}

/* ----- Phase 4 AI / kline ----- */

export type AiSignal = 'strong_buy' | 'buy' | 'hold' | 'sell' | 'strong_sell'
export type AiAdvisor = 'trend' | 'contrarian' | 'risk' | 'valuation'

/** GET /quote/kline/{code} */
export interface KlinePoint {
  date: string
  close: number
}
export interface KlineResp {
  code: string
  days: number
  points: KlinePoint[]
}

/** GET /indices/history fear/greed/heat 历史序列(每个 {date,value})*/
export interface IndexHistoryPoint {
  date: string
  value: number
}
export interface IndicesHistoryResp {
  fear: IndexHistoryPoint[]
  greed: IndexHistoryPoint[]
  heat: IndexHistoryPoint[]
}

/** AI 工具调用记录（result 是人类可读文本）*/
export interface AiToolUsed {
  tool: string
  args: Record<string, any>
  result: string
}

/** AI 顾问意见（注意字段是 score,不是 score_adjustment — synthesis.py:50）*/
export interface AiOpinion {
  advisor: AiAdvisor
  signal: AiSignal
  score: number
  reasoning: string
  tools_used: AiToolUsed[]
}

/** AI 顾问数据上下文（估值顾问 tools 空时走此分支）*/
export interface AiContext {
  pe_pct?: number | null
  pb_pct?: number | null
  dividend_yield?: number | null
  fear?: number | null
  greed?: number | null
  heat?: number | null
  [k: string]: any
}

/** AI 综合决策（后端 aggregate 只含这 3 字段；opinions 在顶层 AiAnalyzeResp.opinions，不内嵌）*/
export interface AiAggregate {
  final_signal: AiSignal
  total_score: number
  risk_vetoed: boolean
}

/** POST /ai/{code} 返回 / GET /ai/result 的 result */
export interface AiAnalyzeResp {
  code: string
  name: string
  context: AiContext
  opinions: AiOpinion[]
  aggregate: AiAggregate
  narrative: string
}

/** GET /ai/result/{code}（none/pending/done 三态；signal 让按钮零请求上色）*/
export interface AiResultResp {
  status: 'none' | 'pending' | 'done'
  result?: AiAnalyzeResp | null
  signal?: AiSignal | null
  analyzed_at?: string
  pending_since?: string
}
