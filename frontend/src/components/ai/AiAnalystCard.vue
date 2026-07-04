<script setup lang="ts">
import { computed } from 'vue'
import { metricsForOpinion } from '@/composables/useAiMetric'
import { analystChart } from '@/composables/useAnalystChart'
import type { AiOpinion, AiContext, AiSignal, AiAdvisor, IndicesHistoryResp } from '@/api/types'

const props = defineProps<{
  opinion: AiOpinion
  context?: AiContext | null
  closes: number[]
  history?: IndicesHistoryResp | null
}>()

// 身份色 = 视角身份编码(4 位分析师靠图标形状 + 背景色区分)
const META: Record<AiAdvisor, { name: string; role: string; color: string }> = {
  trend: { name: '趋势分析师', role: '技术面分析', color: '#2563EB' },
  contrarian: { name: '逆向分析师', role: '情绪面分析', color: '#8B5CF6' },
  risk: { name: '风险分析师', role: '风险面分析', color: '#F97316' },
  valuation: { name: '估值分析师', role: '估值面分析', color: '#10B981' },
}
const SIGNAL_TEXT: Record<AiSignal, string> = {
  strong_buy: 'STRONG BUY',
  buy: 'BUY',
  hold: 'HOLD',
  sell: 'SELL',
  strong_sell: 'STRONG SELL',
}
// A 股:看多红 / 看空绿 / 持有棕
function sigColor(s: AiSignal): string {
  if (s === 'buy' || s === 'strong_buy') return '#E5484D'
  if (s === 'sell' || s === 'strong_sell') return '#16A34A'
  return '#8B7355'
}
function sigBg(s: AiSignal): string {
  if (s === 'buy' || s === 'strong_buy') return '#FDECEC'
  if (s === 'sell' || s === 'strong_sell') return '#EAF8F0'
  return '#F7F2EA'
}

const meta = computed(() => META[props.opinion.advisor] || META.trend)
const metrics = computed(() => metricsForOpinion(props.opinion, props.context))
const scoreSign = computed(() => (props.opinion.score > 0 ? '+' : ''))

// ---- 专属核心指标图 ----
const spec = computed(() =>
  analystChart(props.opinion.advisor, {
    closes: props.closes,
    fear: props.history?.fear,
    greed: props.history?.greed,
    heat: props.history?.heat,
    context: props.context,
  }),
)

interface PathSpec { d: string; color: string; dash?: boolean }
const W = 100
const H = 40
const PAD = 3
// 多线几何:统一 X 轴(按最长序列 n),Y 按 yDomain 或 auto,area 线生成渐变填充
const geom = computed(() => {
  const s = spec.value
  if (!s.lines.length || s.empty) return null
  const n = Math.max(...s.lines.map((l) => l.data.length), 1)
  let mn: number
  let mx: number
  if (s.yDomain) {
    ;[mn, mx] = s.yDomain
  } else {
    mn = Infinity
    mx = -Infinity
    for (const l of s.lines) {
      for (const v of l.data) {
        if (v != null && isFinite(v)) {
          if (v < mn) mn = v
          if (v > mx) mx = v
        }
      }
    }
    const pad = (mx - mn) * 0.1 || 1
    mn -= pad
    mx += pad
  }
  const range = mx - mn || 1
  const toPath = (data: (number | null)[]): string => {
    let d = ''
    let started = false
    for (let i = 0; i < data.length; i++) {
      const v = data[i]
      if (v == null || !isFinite(v)) {
        started = false
        continue
      }
      const x = n > 1 ? (i / (n - 1)) * W : 0
      const y = PAD + (H - 2 * PAD) * (1 - (v - mn) / range)
      d += `${started ? 'L' : 'M'}${x.toFixed(2)},${y.toFixed(2)} `
      started = true
    }
    return d.trim()
  }
  const linePaths: PathSpec[] = s.lines.map((l) => ({ d: toPath(l.data), color: l.color, dash: l.dash }))
  // area:带 area 标志的线(趋势股价)
  const areaLine = s.lines.find((l) => l.area)
  let areaPath = ''
  let areaColor = '#888'
  if (areaLine && areaLine.data.length) {
    const lp = toPath(areaLine.data)
    if (lp) {
      let firstIdx = -1
      let lastIdx = -1
      areaLine.data.forEach((v, i) => {
        if (v != null && isFinite(v)) {
          if (firstIdx < 0) firstIdx = i
          lastIdx = i
        }
      })
      const xFirst = firstIdx >= 0 ? (firstIdx / (n - 1)) * W : 0
      const xLast = lastIdx >= 0 ? (lastIdx / (n - 1)) * W : W
      areaPath = `${lp} L${xLast.toFixed(2)},${H} L${xFirst.toFixed(2)},${H} Z`
      areaColor = areaLine.color
    }
  }
  return { linePaths, areaPath, areaColor }
})
const gradId = computed(() => `ag-${props.opinion.advisor}`)
</script>

<template>
  <div class="card">
    <!-- ① Header(72px):左身份图标 / 右 Badge+评分 -->
    <div class="head">
      <span class="avatar" :style="{ background: meta.color + '14', color: meta.color }">
        <svg v-if="opinion.advisor === 'trend'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <polyline points="3,17 9,11 13,15 21,7" /><polyline points="15,7 21,7 21,13" />
        </svg>
        <svg v-else-if="opinion.advisor === 'contrarian'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
          <circle cx="6" cy="18" r="2" fill="currentColor" /><circle cx="12" cy="6" r="2" fill="currentColor" /><circle cx="18" cy="14" r="2" fill="currentColor" />
          <line x1="6" y1="18" x2="12" y2="6" /><line x1="12" y1="6" x2="18" y2="14" />
        </svg>
        <svg v-else-if="opinion.advisor === 'risk'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" stroke-linecap="round">
          <path d="M12 3 L20 6 V12 C20 16 16 20 12 21 C8 20 4 16 4 12 V6 Z" /><line x1="12" y1="9" x2="12" y2="13.5" /><circle cx="12" cy="16.5" r="0.9" fill="currentColor" />
        </svg>
        <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="12" r="8" /><path d="M12 4 V12 L18 12" />
        </svg>
      </span>
      <div class="head-right">
        <span class="badge" :style="{ color: sigColor(opinion.signal), background: sigBg(opinion.signal) }">
          {{ SIGNAL_TEXT[opinion.signal] }}
        </span>
        <span class="score" :style="{ color: sigColor(opinion.signal) }">{{ scoreSign }}{{ opinion.score }}<small>分</small></span>
      </div>
    </div>

    <!-- ② 分析师信息 -->
    <div class="info">
      <div class="name">{{ meta.name }}</div>
      <div class="role">{{ meta.role }}</div>
    </div>

    <!-- ③ 核心观点(紫标题 + 浅灰卡) -->
    <div class="block">
      <div class="block-title purple">核心观点</div>
      <div class="opinion-box">{{ opinion.reasoning }}</div>
    </div>

    <!-- ④ 关键指标(34px 行高 + 极浅 divider) -->
    <div class="block">
      <div class="block-title">关键指标</div>
      <div class="metrics">
        <div v-for="m in metrics" :key="m.name" class="metric-row">
          <span class="m-name">{{ m.name }}</span>
          <span class="m-value">{{ m.value }}</span>
        </div>
      </div>
    </div>

    <!-- ⑤ 专属核心指标趋势图(每位分析师不同) -->
    <div class="spark">
      <template v-if="spec.gauge">
        <div class="spark-legend">
          <span style="color: #10b981">● PE 分位</span>
          <span style="color: #3b82f6">● PB 分位</span>
          <span v-if="spec.curLabel" class="spark-cur">{{ spec.curLabel }}</span>
        </div>
        <div class="gauge">
          <div class="gauge-track"></div>
          <div v-if="spec.gauge.pe != null" class="gauge-mark pe" :style="{ left: spec.gauge.pe + '%' }">
            <span class="dot"></span><span class="lbl">PE {{ spec.gauge.pe }}%</span>
          </div>
          <div v-if="spec.gauge.pb != null" class="gauge-mark pb" :style="{ left: spec.gauge.pb + '%' }">
            <span class="dot"></span><span class="lbl">PB {{ spec.gauge.pb }}%</span>
          </div>
        </div>
        <div class="gauge-axis"><span>0%</span><span class="hint">便宜 ← 历史 → 贵</span><span>100%</span></div>
      </template>
      <template v-else-if="spec.empty">
        <div class="spark-empty">
          <div class="empty-title">{{ spec.empty.title }}</div>
          <div v-if="spec.empty.sub" class="empty-sub">{{ spec.empty.sub }}</div>
        </div>
      </template>
      <template v-else-if="geom">
        <div class="spark-legend">
          <span v-for="(l, i) in spec.lines" :key="i" :style="{ color: l.color }">● {{ l.label }}</span>
          <span v-if="spec.curLabel" class="spark-cur">{{ spec.curLabel }}</span>
        </div>
        <svg viewBox="0 0 100 40" preserveAspectRatio="none">
          <defs>
            <linearGradient :id="gradId" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" :stop-color="geom.areaColor" stop-opacity="0.22" />
              <stop offset="100%" :stop-color="geom.areaColor" stop-opacity="0" />
            </linearGradient>
          </defs>
          <path v-if="geom.areaPath" :d="geom.areaPath" :fill="`url(#${gradId})`" />
          <path
            v-for="(p, i) in geom.linePaths"
            :key="i"
            :d="p.d"
            fill="none"
            :stroke="p.color"
            :stroke-width="p.dash ? 1 : 1.5"
            :stroke-dasharray="p.dash ? '3 2' : ''"
            stroke-linejoin="round"
            stroke-linecap="round"
          />
        </svg>
      </template>
      <div v-else class="spark-empty"><div class="empty-title">行情数据不足</div></div>
    </div>
  </div>
</template>

<style scoped>
.card {
  background: #ffffff;
  border: 1px solid #efefef;
  border-radius: 20px;
  padding: 20px;
  display: flex;
  flex-direction: column;
  min-height: 560px;
  transition: transform 0.18s, box-shadow 0.18s;
}
.card:hover {
  transform: translateY(-4px);
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.06);
}

/* ① Header */
.head {
  height: 72px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.avatar {
  width: 40px;
  height: 40px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.avatar svg {
  width: 22px;
  height: 22px;
}
.head-right {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 6px;
}
.badge {
  font-size: 12px;
  font-weight: 700;
  padding: 4px 10px;
  border-radius: 20px;
  white-space: nowrap;
}
.score {
  font-family: var(--mono);
  font-weight: 700;
  font-size: 15px;
  font-variant-numeric: tabular-nums;
  line-height: 1;
}
.score small {
  font-size: 11px;
  font-weight: 600;
  margin-left: 1px;
}

/* ② 分析师信息 */
.info {
  margin-top: 16px;
}
.name {
  font-size: 18px;
  font-weight: 700;
  color: #222;
  line-height: 1.2;
}
.role {
  font-size: 14px;
  color: #999;
  margin-top: 4px;
}

/* block 通用 */
.block {
  margin-top: 20px;
}
.block-title {
  font-size: 12px;
  font-weight: 700;
  color: #888;
  margin-bottom: 10px;
  letter-spacing: 0.3px;
}
.block-title.purple {
  color: #8b5cf6;
}

/* ③ 核心观点 */
.opinion-box {
  background: #f7f7f8;
  border-radius: 16px;
  padding: 16px;
  font-size: 15px;
  line-height: 1.8;
  color: #444;
}

/* ④ 关键指标 */
.metrics {
  display: flex;
  flex-direction: column;
}
.metric-row {
  height: 34px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid #f4f4f4;
  font-size: 13px;
  gap: 10px;
}
.metric-row:last-child {
  border-bottom: 0;
}
.m-name {
  color: #666;
  flex-shrink: 0;
}
.m-value {
  color: #222;
  font-weight: 600;
  font-family: var(--mono);
  font-variant-numeric: tabular-nums;
  text-align: right;
}

/* ⑤ 专属指标趋势图(吸底) */
.spark {
  margin-top: auto;
  height: 150px;
  display: flex;
  flex-direction: column;
}
.spark-legend {
  display: flex;
  gap: 12px;
  font-size: 11px;
  margin-bottom: 6px;
  align-items: center;
  flex-wrap: wrap;
}
.spark-legend span {
  white-space: nowrap;
}
.spark-cur {
  margin-left: auto;
  font-family: var(--mono);
  color: #444;
  font-weight: 600;
}
.spark svg {
  width: 100%;
  flex: 1;
  display: block;
}
.spark-empty {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  gap: 6px;
}
.empty-title {
  font-size: 13px;
  color: #999;
  font-weight: 600;
}
.empty-sub {
  font-size: 12px;
  color: #bbb;
}

/* 估值分位标尺 */
.gauge {
  position: relative;
  height: 48px;
  margin-top: 8px;
}
.gauge-track {
  position: absolute;
  top: 50%;
  left: 0;
  right: 0;
  height: 6px;
  background: linear-gradient(90deg, #16a34a 0%, #e5e7eb 50%, #e5484d 100%);
  border-radius: 3px;
  transform: translateY(-50%);
}
.gauge-mark {
  position: absolute;
  top: 50%;
  transform: translate(-50%, -50%);
  width: 12px;
  height: 12px;
}
.gauge-mark .dot {
  display: block;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  border: 2px solid #fff;
  box-shadow: 0 0 0 1px rgba(0, 0, 0, 0.12);
}
.gauge-mark.pe .dot {
  background: #10b981;
}
.gauge-mark.pb .dot {
  background: #3b82f6;
}
.gauge-mark .lbl {
  position: absolute;
  left: 50%;
  transform: translateX(-50%);
  font-size: 10px;
  font-family: var(--mono);
  font-weight: 600;
  color: #444;
  white-space: nowrap;
}
.gauge-mark.pe .lbl {
  bottom: calc(100% + 4px);
}
.gauge-mark.pb .lbl {
  top: calc(100% + 4px);
}
.gauge-axis {
  display: flex;
  justify-content: space-between;
  font-size: 10px;
  color: #999;
  margin-top: 6px;
}
.gauge-axis .hint {
  color: #bbb;
}
</style>
