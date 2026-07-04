<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { NModal } from 'naive-ui'
import { aiApi, quoteApi, indicesApi } from '@/api/modules'
import { isError } from '@/api/client'
import AiAnalystCard from './AiAnalystCard.vue'
import type { AiAnalyzeResp, AiOpinion, AiSignal, IndicesHistoryResp } from '@/api/types'

const props = defineProps<{ show: boolean; code: string }>()
const emit = defineEmits<{ 'update:show': [boolean] }>()

const data = ref<AiAnalyzeResp | null>(null)
const analyzedAt = ref('')
const loading = ref(false)
const err = ref('')
const closes = ref<number[]>([])
const closesCache = new Map<string, number[]>()
const history = ref<IndicesHistoryResp | null>(null)
const historyCache = new Map<string, IndicesHistoryResp>()

// 规格要求英文信号词(SELL/BUY/HOLD/STRONG BUY/STRONG SELL)
const SIGNAL_EN: Record<AiSignal, string> = {
  strong_buy: 'STRONG BUY',
  buy: 'BUY',
  hold: 'HOLD',
  sell: 'SELL',
  strong_sell: 'STRONG SELL',
}
function sigColor(s: AiSignal): string {
  if (s === 'buy' || s === 'strong_buy') return '#E5484D' // A 股看多红
  if (s === 'sell' || s === 'strong_sell') return '#16A34A' // 看空绿
  return '#8B7355' // 持有棕
}

const agg = computed(() => data.value?.aggregate || null)
const finalSignal = computed<AiSignal | null>(() => agg.value?.final_signal || null)
// opinions 在顶层 result.opinions(后端 aggregate 只含 final_signal/total_score/risk_vetoed)
const opinions = computed<AiOpinion[]>(() => data.value?.opinions || [])
const riskOpinion = computed(
  () => opinions.value.find((o) => o.advisor === 'risk') || null,
)
// 最终意见大字:短词(BUY/SELL/HOLD)56px,STRONG 系列 34px(180px 栏放不下 56px)
const signalFontSz = computed(() => {
  if (!finalSignal.value) return 52
  return SIGNAL_EN[finalSignal.value].length > 5 ? 34 : 56
})

const scoreLabel = computed(() => {
  const s = agg.value?.total_score ?? 0
  if (s >= 5) return { text: '偏多', color: '#E5484D', bg: '#FDECEC' }
  if (s <= -5) return { text: '偏空', color: '#16A34A', bg: '#EAF8F0' }
  return { text: '中性', color: '#8B7355', bg: '#F7F2EA' }
})

const ACTION: Record<AiSignal, { pos: string; period: string }> = {
  strong_buy: { pos: '80% ~ 100%', period: '中期加仓' },
  buy: { pos: '50% ~ 80%', period: '中期持有' },
  hold: { pos: '30% ~ 50%', period: '短中期观望' },
  sell: { pos: '10% ~ 30%', period: '短期减仓' },
  strong_sell: { pos: '0% ~ 10%', period: '短线观望' },
}
const action = computed(() => (finalSignal.value ? ACTION[finalSignal.value] : null))

const risks = computed<string[]>(() => {
  const r = riskOpinion.value?.reasoning || ''
  if (!r) return []
  return r
    .split(/[;；。\n]+/)
    .map((s) => s.trim())
    .filter(Boolean)
    .slice(0, 3)
})

const narrativeParas = computed<string[]>(() => {
  const n = data.value?.narrative || ''
  if (!n) return []
  const sents = n.split(/(?<=[。！？!?])/).map((s) => s.trim()).filter(Boolean)
  if (sents.length <= 1) return [n]
  const mid = Math.ceil(sents.length / 2)
  return [sents.slice(0, mid).join(''), sents.slice(mid).join('')]
})

async function load() {
  if (!props.code) return
  loading.value = true
  err.value = ''
  const closesCached = closesCache.has(props.code)
  const histCached = historyCache.has(props.code)
  const [rr, kr, hr] = await Promise.all([
    aiApi.result(props.code),
    closesCached ? Promise.resolve(null) : quoteApi.kline(props.code, 120),
    histCached ? Promise.resolve(null) : indicesApi.history('stock', props.code, 30),
  ])
  loading.value = false
  if (isError(rr)) {
    err.value = '加载失败:' + rr.error
    return
  }
  analyzedAt.value = rr.analyzed_at || ''
  if (rr.status === 'done' && rr.result) {
    data.value = rr.result
  } else if (rr.status === 'pending') {
    err.value = '分析进行中,请稍后重开'
  } else {
    err.value = '暂无分析结果,请先点「分析」'
  }
  if (kr && !isError(kr)) {
    const cl = (kr.points || []).map((p) => p.close)
    closesCache.set(props.code, cl)
    closes.value = cl
  } else if (closesCached) {
    closes.value = closesCache.get(props.code) || []
  }
  if (hr && !isError(hr)) {
    historyCache.set(props.code, hr)
    history.value = hr
  } else if (histCached) {
    history.value = historyCache.get(props.code) || null
  }
}

watch(
  () => [props.show, props.code],
  ([show]) => {
    if (show) load()
  },
)
</script>

<template>
  <NModal
    :show="show"
    :mask-closable="true"
    :auto-focus="false"
    @update:show="(v: boolean) => emit('update:show', v)"
  >
    <div class="ai-shell">
      <!-- ① Header(80px):左 股票名+副标题 / 右 关闭 -->
      <header class="ai-header">
        <div class="h-left">
          <span class="h-name">{{ data?.name || code }}</span>
          <span class="h-sub">AI 分析报告</span>
          <span class="h-code">{{ code }}</span>
        </div>
        <button class="x" title="关闭" @click="emit('update:show', false)">✕</button>
      </header>

      <div v-if="loading" class="ai-state"><span class="spin"></span> 加载报告中…</div>
      <div v-else-if="err" class="ai-state err">{{ err }}</div>

      <template v-else-if="data && agg">
        <!-- ② Summary(120px):一张卡,1px 竖线分四栏 -->
        <section class="summary">
          <div class="sum-col col-signal">
            <div class="sum-label">最终意见</div>
            <div
              class="sum-signal"
              :style="{ color: finalSignal ? sigColor(finalSignal) : '#999', fontSize: signalFontSz + 'px' }"
            >
              {{ finalSignal ? SIGNAL_EN[finalSignal] : '—' }}
            </div>
          </div>
          <div class="sum-col col-score">
            <div class="sum-label">综合评分</div>
            <div class="sum-score-row">
              <span class="sum-score">{{ agg.total_score ?? 0 }}<small> / 100</small></span>
              <span class="sum-pill" :style="{ color: scoreLabel.color, background: scoreLabel.bg }">{{ scoreLabel.text }}</span>
            </div>
          </div>
          <div class="sum-col col-risk" :class="{ vetoed: agg.risk_vetoed }">
            <span
              class="risk-icon"
              :style="{ background: agg.risk_vetoed ? '#F97316' : '#F2F2F2', color: agg.risk_vetoed ? '#fff' : '#BBB' }"
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" stroke-linecap="round">
                <path d="M12 3 L22 20 H2 Z" /><line x1="12" y1="10" x2="12" y2="14.5" /><circle cx="12" cy="17.5" r="0.9" fill="currentColor" />
              </svg>
            </span>
            <div class="risk-body">
              <div class="risk-title">{{ agg.risk_vetoed ? '风险顾问 · 一票否决' : '风险顾问' }}</div>
              <div class="risk-text">
                {{ riskOpinion?.reasoning
                  ? riskOpinion.reasoning.slice(0, 30) + (riskOpinion.reasoning.length > 30 ? '…' : '')
                  : '暂无明显风险' }}
              </div>
            </div>
          </div>
          <div class="sum-col col-meta">
            <div class="meta-row"><span>分析时间</span><b>{{ analyzedAt || '近期' }}</b></div>
            <div class="meta-row"><span>数据来源</span><b>历史数据</b></div>
          </div>
        </section>

        <!-- ③ Analyst:4 列 grid,每卡五层 -->
        <section class="cards">
          <AiAnalystCard
            v-for="op in opinions"
            :key="op.advisor"
            :opinion="op"
            :context="data.context"
            :closes="closes"
            :history="history"
          />
        </section>

        <!-- ④ Conclusion:左 70% 综合结论 / 右 30% 操作建议 -->
        <section class="bottom">
          <div class="bot-left">
            <div class="bot-title">综合结论</div>
            <div class="bot-narr">
              <p v-for="(p, i) in narrativeParas" :key="i">{{ p }}</p>
              <p v-if="!narrativeParas.length" class="muted">暂无综合解读</p>
            </div>
          </div>
          <div class="bot-right">
            <div class="bot-title">操作建议</div>
            <div class="bot-signal" :style="{ color: finalSignal ? sigColor(finalSignal) : '#999' }">
              {{ finalSignal ? SIGNAL_EN[finalSignal] : '—' }}
            </div>
            <div class="bot-grid">
              <div class="bot-cell"><span>建议仓位</span><b>{{ action?.pos || '—' }}</b></div>
              <div class="bot-cell"><span>建议周期</span><b>{{ action?.period || '—' }}</b></div>
            </div>
            <div class="bot-risk-title">核心风险</div>
            <ul class="bot-risk">
              <li v-for="(r, i) in risks" :key="i">{{ r }}</li>
              <li v-if="!risks.length" class="muted">暂无</li>
            </ul>
          </div>
        </section>
      </template>
    </div>
  </NModal>
</template>

<style scoped>
/* 固定浅色字面量(不引用主题 var),深色主题下弹窗仍纯白 */
.ai-shell {
  width: min(96vw, 1520px);
  height: min(94vh, 920px);
  overflow-y: auto;
  background: #ffffff;
  border-radius: 24px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.08);
  padding: 32px;
  font-family: var(--sans);
  color: #222;
}

/* ① Header */
.ai-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 80px;
  margin-bottom: 24px;
}
.h-left {
  display: flex;
  align-items: baseline;
  gap: 16px;
  min-width: 0;
}
.h-name {
  font-size: 32px;
  font-weight: 700;
  color: #222;
  line-height: 1.1;
}
.h-sub {
  font-size: 26px;
  font-weight: 600;
  color: #333;
}
.h-code {
  font-family: var(--mono);
  font-size: 14px;
  font-weight: 500;
  color: #999;
}
.x {
  width: 40px;
  height: 40px;
  border-radius: 10px;
  border: 0;
  background: transparent;
  color: #999;
  font-size: 17px;
  cursor: pointer;
  transition: 0.15s;
  flex-shrink: 0;
}
.x:hover {
  background: #f5f5f5;
  color: #222;
}

.ai-state {
  padding: 80px 0;
  text-align: center;
  color: #666;
  font-size: 15px;
}
.ai-state.err {
  color: #e5484d;
}
.spin {
  display: inline-block;
  width: 16px;
  height: 16px;
  border: 2px solid #ddd;
  border-top-color: #888;
  border-radius: 50%;
  animation: ai-spin 0.7s linear infinite;
  vertical-align: middle;
  margin-right: 8px;
}
@keyframes ai-spin {
  to {
    transform: rotate(360deg);
  }
}

/* ② Summary 四竖线栏 */
.summary {
  display: grid;
  grid-template-columns: 180px 260px 1fr 250px;
  background: #ffffff;
  border-radius: 20px;
  padding: 24px;
  margin-bottom: 28px;
  align-items: center;
  min-height: 120px;
}
.sum-col {
  padding: 0 20px;
  min-width: 0;
}
.sum-col:first-child {
  padding-left: 0;
}
.sum-col:not(:first-child) {
  border-left: 1px solid #efefef;
}
.sum-col:last-child {
  padding-right: 0;
}
.sum-label {
  font-size: 12px;
  color: #999;
  margin-bottom: 10px;
  letter-spacing: 0.3px;
}
.sum-signal {
  font-weight: 700;
  line-height: 1;
  letter-spacing: -0.5px;
}
.col-score {
  /* 上下布局:label / 数字+胶囊 */
}
.sum-score-row {
  display: flex;
  align-items: center;
  gap: 14px;
}
.sum-score {
  font-family: var(--mono);
  font-size: 54px;
  font-weight: 700;
  color: #222;
  line-height: 1;
  font-variant-numeric: tabular-nums;
}
.sum-score small {
  font-size: 16px;
  color: #999;
  font-weight: 500;
}
.sum-pill {
  font-size: 13px;
  font-weight: 700;
  padding: 6px 14px;
  border-radius: 20px;
  white-space: nowrap;
}
.col-risk {
  display: flex;
  align-items: center;
  gap: 18px;
}
.risk-icon {
  width: 56px;
  height: 56px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.risk-icon svg {
  width: 28px;
  height: 28px;
}
.risk-title {
  font-size: 18px;
  font-weight: 700;
  color: #222;
  margin-bottom: 4px;
}
.col-risk.vetoed .risk-title {
  color: #f97316;
}
.risk-text {
  font-size: 16px;
  color: #888;
  line-height: 1.4;
}
.col-meta {
  text-align: right;
}
.meta-row {
  font-size: 15px;
  color: #666;
  margin-bottom: 8px;
}
.meta-row:last-child {
  margin-bottom: 0;
}
.meta-row b {
  color: #666;
  font-weight: 600;
  margin-left: 6px;
  font-family: var(--mono);
  font-variant-numeric: tabular-nums;
}

/* ③ Analyst 4 卡 */
.cards {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 20px;
  margin-bottom: 28px;
  align-items: stretch;
}

/* ④ Conclusion */
.bottom {
  display: grid;
  grid-template-columns: 7fr 3fr;
  gap: 20px;
}
.bot-left,
.bot-right {
  background: #ffffff;
  border: 1px solid #ececec;
  border-radius: 20px;
  padding: 24px;
  min-height: 180px;
}
.bot-left {
  border-left: 4px solid #16a34a;
}
.bot-title {
  font-weight: 700;
  color: #222;
  margin-bottom: 14px;
  letter-spacing: 0.3px;
}
.bot-left .bot-title {
  font-size: 20px;
}
.bot-right .bot-title {
  font-size: 18px;
}
.bot-narr p {
  font-size: 16px;
  line-height: 1.9;
  color: #444;
  margin-bottom: 12px;
}
.bot-narr p:last-child {
  margin-bottom: 0;
}
.bot-signal {
  font-size: 48px;
  font-weight: 700;
  line-height: 1;
  margin-bottom: 16px;
  letter-spacing: -0.5px;
}
.bot-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  margin-bottom: 16px;
}
.bot-cell {
  background: #fafafa;
  border-radius: 10px;
  padding: 10px 12px;
}
.bot-cell span {
  display: block;
  font-size: 11px;
  color: #999;
  margin-bottom: 4px;
}
.bot-cell b {
  font-size: 14px;
  color: #222;
  font-weight: 700;
}
.bot-risk-title {
  font-size: 12px;
  font-weight: 700;
  color: #666;
  margin-bottom: 8px;
}
.bot-risk {
  list-style: none;
  padding: 0;
  margin: 0;
}
.bot-risk li {
  font-size: 13px;
  color: #555;
  line-height: 1.7;
  padding-left: 14px;
  position: relative;
}
.bot-risk li::before {
  content: '•';
  position: absolute;
  left: 2px;
  color: #f97316;
}
.muted {
  color: #bbb;
}
@media (max-width: 1200px) {
  .cards {
    grid-template-columns: repeat(2, 1fr);
  }
  .summary {
    grid-template-columns: 1fr 1fr;
    row-gap: 20px;
  }
  .sum-col:not(:first-child) {
    border-left: 0;
  }
  .bottom {
    grid-template-columns: 1fr;
  }
}
</style>
