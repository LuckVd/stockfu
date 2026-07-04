// A 股色 token + 顾问身份色。Phase 1 与旧 index.html 的 CSS 变量对齐(复用);
// Phase 4 AI 弹窗会在此扩展机构级局部色(红 #E5484D / 绿 #16A34A / 棕 #8B7355)。
export const COLOR = {
  UP: '#dc2626',      // 看多/涨/买入(对齐旧 --up)
  DN: '#16a34a',      // 看空/跌/卖出(对齐旧 --down)
  NEU: '#64748b',     // 持有/中性
} as const

export const ADVISOR_COLOR = {
  trend: '#2563eb',       // 趋势 蓝
  contrarian: '#8b5cf6',  // 逆向 紫
  risk: '#f97316',        // 风险 橙(Phase 4 用,区别于旧红)
  valuation: '#10b981',   // 估值 绿
} as const

export type Signal = 'strong_buy' | 'buy' | 'hold' | 'sell' | 'strong_sell'

export function signalColor(sig: string): string {
  if (sig === 'buy' || sig === 'strong_buy') return COLOR.UP
  if (sig === 'sell' || sig === 'strong_sell') return COLOR.DN
  return COLOR.NEU
}
