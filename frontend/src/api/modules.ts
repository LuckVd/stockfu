// 端点分组。Phase 1 indices；Phase 2 portfolio/watchlist/sectors/holding/watch；Phase 3 trade/stock/config/csv。
import { doGet, doPost, doPut, doDel, BASE_URL } from './client'
import type { ApiErr } from './client'
import type {
  PortfolioResp,
  WatchlistItem,
  FlowResp,
  DelHoldingResp,
  DelWatchResp,
  StockIndexResp,
  TradeResp,
  WatchAddResp,
  EnsureResp,
  ProxyConfig,
  ProxyTestResult,
  ScheduleConfig,
  MailConfig,
  MailTestResult,
  LlmConfig,
  LlmTestResult,
  SignalConfig,
  SignalSubscriptionsResp,
  SignalReport,
  CsvImportResult,
  AiAnalyzeResp,
  AiResultResp,
  KlineResp,
  IndicesHistoryResp,
} from './types'

export const indicesApi = {
  quotes: () => doGet('/indices/quotes'),
  stock: (code: string) => doGet<StockIndexResp>('/indices/stock/' + encodeURIComponent(code)),
  history: (level: string, scope: string, days = 30) =>
    doGet<IndicesHistoryResp>(`/indices/history?level=${level}&scope=${encodeURIComponent(scope)}&days=${days}`),
}

export const portfolioApi = {
  list: () => doGet<PortfolioResp>('/portfolio'),
}

export const watchlistApi = {
  list: () => doGet<WatchlistItem[]>('/watchlist'),
}

export const sectorsApi = {
  flow: (top_n = 90) => doGet<FlowResp>(`/sectors/flow?top_n=${top_n}`),
}

export const holdingApi = {
  del: (code: string) => doDel<DelHoldingResp>('/holding/' + encodeURIComponent(code)),
}

export const watchApi = {
  add: (code: string) => doPost<WatchAddResp>('/watch/' + encodeURIComponent(code)),
  del: (code: string) => doDel<DelWatchResp>('/watch/' + encodeURIComponent(code)),
}

export const tradeApi = {
  trade: (b: { code: string; side: 'buy' | 'sell'; shares: number; price: number; date?: string }) =>
    doPost<TradeResp>('/trade', b),
}

export const stockApi = {
  ensure: (code: string) => doPost<EnsureResp>('/stock/' + encodeURIComponent(code) + '/ensure'),
}

// ---- Phase 3 config ----
export const configApi = {
  getProxy: () => doGet<ProxyConfig>('/config/proxy'),
  setProxy: (b: { proxy_url: string }) => doPut<{ ok: true; effective: string }>('/config/proxy', b),
  testProxy: (b?: { proxy_url?: string }) => doPost<ProxyTestResult>('/config/proxy/test', b || {}),
  getSchedule: () => doGet<ScheduleConfig>('/config/schedule'),
  setSchedule: (b: Partial<{ daily_fetch_time: string; fetch_retry_interval: number; fetch_retry_count: number }>) =>
    doPut('/config/schedule', b),
  getMail: () => doGet<MailConfig>('/config/mail'),
  setMail: (b: Partial<{
    smtp_host: string; smtp_port: number; smtp_user: string; smtp_pass: string
    smtp_from: string; mail_to: string; mail_enabled: boolean; mail_time: string; mail_days: string
  }>) => doPut<MailConfig>('/config/mail', b),
  testMail: () => doPost<MailTestResult>('/config/mail/test'),
  getLlm: () => doGet<LlmConfig>('/config/llm'),
  setLlm: (b: Partial<{ llm_base_url: string; llm_model: string; llm_api_key: string }>) =>
    doPut<LlmConfig>('/config/llm', b),
  testLlm: () => doPost<LlmTestResult>('/config/llm/test'),
}

export const signalApi = {
  getConfig: () => doGet<SignalConfig>('/signals/config'),
  setConfig: (b: Partial<Pick<SignalConfig,
    'factor_enabled' | 'llm_enabled' | 'mail_enabled' | 'scan_time' | 'strategy_ids'>>) =>
    doPut<SignalConfig>('/signals/config', b),
  subscriptions: () => doGet<SignalSubscriptionsResp>('/signals/subscriptions'),
  setSubscriptions: (updates: Array<{
    code: string; factor_mail_enabled?: boolean; llm_enabled?: boolean
  }>) => doPut<{ updated: number }>('/signals/subscriptions', { updates }),
  latest: (allResults = false) => doGet<SignalReport>(`/signals/latest?all_results=${allResults ? 'true' : 'false'}`),
  testMail: () => doPost<MailTestResult>('/signals/mail/test'),
}

// ---- Phase 3 csv ----
type CsvScope = 'holdings' | 'watchlist'

export const csvApi = {
  importScope: (scope: CsvScope, file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return doPost<CsvImportResult>(`/csv/import/${scope}`, fd, false)
  },
  async exportScope(scope: CsvScope): Promise<{ filename: string; rows: number } | ApiErr> {
    const res = await fetch(BASE_URL + `/csv/export/${scope}`)
    if (!res.ok) return { error: 'HTTP ' + res.status }
    const txt = await res.text()
    const m = (res.headers.get('content-disposition') || '').match(/filename="?([^"]+)"?/)
    const filename = m ? m[1] : scope + '.csv'
    const a = document.createElement('a')
    a.href = URL.createObjectURL(new Blob([txt], { type: 'text/csv' }))
    a.download = filename
    a.click()
    URL.revokeObjectURL(a.href)
    return { filename, rows: txt.trim() ? txt.trim().split(/\r?\n/).length - 1 : 0 }
  },
  templateUrl: (scope: CsvScope) => BASE_URL + `/csv/template/${scope}`,
}

// ---- Phase 4 AI / kline ----
export const aiApi = {
  run: (code: string) => doPost<AiAnalyzeResp>('/ai/' + encodeURIComponent(code)),
  result: (code: string) => doGet<AiResultResp>('/ai/result/' + encodeURIComponent(code)),
}

export const quoteApi = {
  kline: (code: string, days = 30) =>
    doGet<KlineResp>(`/quote/kline/${encodeURIComponent(code)}?days=${days}`),
}
