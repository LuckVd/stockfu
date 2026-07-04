// AI 工具结果 →「指标名→判断」解析。
// 策略:tool 名定指标名 + result 关键词归类(不硬正则数值);失败回退中文名+摘要,永不空白。
// 估值顾问 tools_used 为空 → 走 context.pe_pct/pb_pct/股息率。
import type { AiOpinion, AiContext, AiToolUsed } from '@/api/types'

export interface Metric {
  name: string
  value: string
}

const TOOL_NAME: Record<string, string> = {
  ma_alignment: 'MA5/20/60/120',
  macd: 'MACD',
  rsi: 'RSI',
  volume_price: '成交量',
  bollinger: '布林带',
  volatility: '波动率',
  support_resistance: '支撑/阻力',
}

function classify(tool: string, result: string): string {
  const r = result || ''
  if (tool === 'ma_alignment') {
    if (/空头排列/.test(r)) return '空头排列'
    if (/多头排列/.test(r)) return '多头排列'
    return /向下|下行/.test(r) ? '均线向下' : '均线走平'
  }
  if (tool === 'macd') {
    if (/金叉/.test(r)) return '金叉向上'
    if (/死叉/.test(r)) return '死叉向下'
    return /零轴上方/.test(r) ? '零轴上方' : '零轴下方'
  }
  if (tool === 'rsi') {
    const m = r.match(/RSI[^=]*=\s*([\d.]+)/)
    const v = m ? m[1] : ''
    if (/超买/.test(r)) return v ? `${v} 超买` : '超买'
    if (/超卖/.test(r)) return v ? `${v} 超卖` : '超卖'
    if (/偏强/.test(r)) return v ? `${v} 偏强` : '偏强'
    if (/偏弱/.test(r)) return v ? `${v} 偏弱` : '偏弱'
    return v ? `${v} 中性` : '中性'
  }
  if (tool === 'volume_price') {
    if (/放量上涨/.test(r)) return '放量上涨'
    if (/缩量下跌/.test(r)) return '缩量下跌'
    if (/放量/.test(r)) return '放量'
    if (/缩量/.test(r)) return '缩量'
    return '量价正常'
  }
  if (tool === 'bollinger') {
    if (/上轨|超买/.test(r)) return '触及上轨'
    if (/下轨|超卖/.test(r)) return '触及下轨'
    return '中轨附近'
  }
  if (tool === 'volatility') {
    if (/高分位|高波动|异常波动/.test(r)) return '高分位·高波动'
    if (/低分位|低波动/.test(r)) return '低分位·低波动'
    return '正常'
  }
  if (tool === 'support_resistance') {
    if (/接近阻力|触及阻力|距.{0,4}阻力/.test(r)) return '接近阻力'
    if (/接近支撑|触及支撑|距.{0,4}支撑/.test(r)) return '接近支撑'
    return '中部区间'
  }
  return ''
}

function toolToMetric(t: AiToolUsed): Metric {
  const name = TOOL_NAME[t.tool] || t.tool
  const judged = classify(t.tool, t.result)
  return { name, value: judged || (t.result || '').slice(0, 30) }
}

// 一个顾问的指标列表
export function metricsForOpinion(op: AiOpinion, ctx?: AiContext | null): Metric[] {
  if (op.advisor === 'valuation') {
    const out: Metric[] = []
    if (ctx) {
      if (ctx.pe_pct != null) out.push({ name: 'PE 分位', value: ctx.pe_pct.toFixed(1) + '%' })
      if (ctx.pb_pct != null) out.push({ name: 'PB 分位', value: ctx.pb_pct.toFixed(1) + '%' })
      if (ctx.dividend_yield != null) out.push({ name: '股息率', value: ctx.dividend_yield.toFixed(2) + '%' })
    }
    return out.length ? out : [{ name: '估值', value: '样本不足' }]
  }
  if (!op.tools_used || !op.tools_used.length) {
    return [{ name: '指标', value: '样本不足' }]
  }
  return op.tools_used.map(toolToMetric)
}
