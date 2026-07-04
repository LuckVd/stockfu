// 4 位分析师专属核心指标趋势图:算法 + 按 advisor 组装 ChartSpec。
// 数据诚实:序列短就照画(逆向 fear/greed 当前点少会攒长),数据缺就 empty 空态,绝不造假。
import type { AiAdvisor, AiContext } from '@/api/types'
import type { IndexHistoryPoint } from '@/api/types'

export interface LineSpec {
  label: string
  data: (number | null)[]   // 与序列同长,前若干为 null(如 MA60 前 59 个)
  color: string
  dash?: boolean            // 虚线(MA)
  area?: boolean            // 渐变填充(仅趋势股价)
}

export interface ChartSpec {
  legend: string[]
  lines: LineSpec[]
  yDomain: [number, number] | null  // null=auto(价格/波动率);[0,100]=情绪/RSI 固定
  curLabel?: string                  // 末端当前值标注
  empty?: { title: string; sub?: string }
  gauge?: { pe: number | null; pb: number | null; dividend_yield?: number | null } // 估值分位标尺
}

export interface AnalystChartData {
  closes: number[]
  fear?: IndexHistoryPoint[]
  greed?: IndexHistoryPoint[]
  heat?: IndexHistoryPoint[]
  context?: AiContext | null
}

// 身份色(与 AiAnalystCard META 对齐)
const C = {
  trend: '#2563EB',
  contrarian: '#8B5CF6',
  risk: '#F97316',
  valuation: '#10B981',
}

// ---- 指标算法 ----

/** 简单移动平均:等长数组,前 period-1 个为 null */
export function sma(values: number[], period: number): (number | null)[] {
  const out: (number | null)[] = []
  let sum = 0
  for (let i = 0; i < values.length; i++) {
    sum += values[i]
    if (i >= period) sum -= values[i - period]
    out.push(i >= period - 1 ? sum / period : null)
  }
  return out
}

/** 20 日滚动年化波动率(日收益 std × √250,单位 %) */
export function rollingVolatility(closes: number[], window = 20): (number | null)[] {
  const out: (number | null)[] = []
  const rets: number[] = [0]
  for (let i = 1; i < closes.length; i++) {
    rets.push(closes[i - 1] > 0 ? Math.log(closes[i] / closes[i - 1]) : 0)
  }
  for (let i = 0; i < closes.length; i++) {
    if (i < window) {
      out.push(null)
      continue
    }
    const w = rets.slice(i - window + 1, i + 1)
    const mean = w.reduce((a, b) => a + b, 0) / w.length
    const variance = w.reduce((a, b) => a + (b - mean) ** 2, 0) / Math.max(1, w.length - 1)
    out.push(Math.sqrt(variance) * Math.sqrt(250) * 100)
  }
  return out
}

/** RSI(Wilder 平滑) */
export function rsi(closes: number[], period = 14): (number | null)[] {
  const out: (number | null)[] = []
  let avgGain = 0
  let avgLoss = 0
  for (let i = 0; i < closes.length; i++) {
    if (i === 0) {
      out.push(null)
      continue
    }
    const ch = closes[i] - closes[i - 1]
    const gain = Math.max(ch, 0)
    const loss = Math.max(-ch, 0)
    if (i <= period) {
      avgGain += gain
      avgLoss += loss
      if (i === period) {
        avgGain /= period
        avgLoss /= period
        out.push(avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss))
      } else {
        out.push(null)
      }
    } else {
      avgGain = (avgGain * (period - 1) + gain) / period
      avgLoss = (avgLoss * (period - 1) + loss) / period
      out.push(avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss))
    }
  }
  return out
}

// ---- helpers ----

function fmt(n: number | null | undefined): string {
  if (n == null || !isFinite(n)) return '—'
  if (Math.abs(n) >= 100) return n.toFixed(0)
  if (Math.abs(n) >= 10) return n.toFixed(1)
  return n.toFixed(2)
}

function lastNonNull(arr: (number | null)[]): number | null {
  for (let i = arr.length - 1; i >= 0; i--) {
    const v = arr[i]
    if (v != null && isFinite(v)) return v
  }
  return null
}

// ---- 按 advisor 组装 ChartSpec ----

export function analystChart(advisor: AiAdvisor, data: AnalystChartData): ChartSpec {
  const closes = data.closes || []

  if (advisor === 'trend') {
    const lines: LineSpec[] = [{ label: '股价', data: closes, color: C.trend, area: true }]
    if (closes.length >= 20) {
      lines.push({ label: 'MA20', data: sma(closes, 20), color: '#93C5FD', dash: true })
    }
    if (closes.length >= 60) {
      lines.push({ label: 'MA60', data: sma(closes, 60), color: '#9CA3AF', dash: true })
    }
    return {
      legend: lines.map((l) => l.label),
      lines,
      yDomain: null,
      curLabel: closes.length ? `¥${fmt(closes[closes.length - 1])}` : undefined,
    }
  }

  if (advisor === 'contrarian') {
    const fv = (data.fear || []).map((p) => p.value)
    const gv = (data.greed || []).map((p) => p.value)
    if (fv.length >= 5 || gv.length >= 5) {
      const lines: LineSpec[] = []
      if (fv.length) lines.push({ label: 'Fear', data: fv, color: '#E5484D' })
      if (gv.length) lines.push({ label: 'Greed', data: gv, color: '#16A34A' })
      return {
        legend: lines.map((l) => l.label),
        lines,
        yDomain: [0, 100],
        curLabel: fv.length ? `Fear ${fmt(fv[fv.length - 1])}` : undefined,
      }
    }
    // fear/greed 历史太短 → 降级 RSI(从 K 线算)
    if (closes.length >= 15) {
      const r = rsi(closes, 14)
      return {
        legend: ['RSI(14)'],
        lines: [{ label: 'RSI', data: r, color: C.contrarian }],
        yDomain: [0, 100],
        curLabel: `RSI ${fmt(lastNonNull(r))}`,
      }
    }
    return { legend: [], lines: [], yDomain: null, empty: { title: '情绪历史样本不足', sub: 'fear/greed 序列待攒' } }
  }

  if (advisor === 'risk') {
    if (closes.length >= 25) {
      const vol = rollingVolatility(closes, 20)
      const cur = data.context?.volatility_pct ?? lastNonNull(vol)
      return {
        legend: ['30 日波动率'],
        lines: [{ label: '波动率', data: vol, color: C.risk }],
        yDomain: null,
        curLabel: `${fmt(cur)}%`,
      }
    }
    return { legend: [], lines: [], yDomain: null, empty: { title: '波动率样本不足' } }
  }

  // valuation:有 PE/PB 分位 → 标尺;都无 → 空态(股息率)
  const pe = data.context?.pe_pct ?? null
  const pb = data.context?.pb_pct ?? null
  const dy = data.context?.dividend_yield ?? null
  if (pe == null && pb == null) {
    return {
      legend: [], lines: [], yDomain: null,
      empty: { title: 'PE/PB 历史样本不足', sub: dy != null ? `当前股息率 ${fmt(dy)}%` : '接 tushare 后补全估值分位' },
    }
  }
  return {
    legend: [], lines: [], yDomain: null,
    gauge: { pe, pb, dividend_yield: dy },
    curLabel: `PE ${fmt(pe)}% · PB ${fmt(pb)}%`,
  }
}
