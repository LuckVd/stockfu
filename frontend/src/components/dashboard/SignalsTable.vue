<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { signalApi } from '@/api/modules'
import { isError } from '@/api/client'
import type { SignalReport } from '@/api/types'

const loading = ref(false)
const err = ref('')
const report = ref<SignalReport | null>(null)
const showAll = ref(false)

function scoreText(value: number | null) {
  return value == null ? '—' : value.toFixed(1)
}
function scoreClass(value: number | null) {
  if (value == null) return 'neutral'
  if (value >= 60) return 'up'
  if (value <= 40) return 'down'
  return 'neutral'
}
function barWidth(value: number | null) {
  return `${Math.max(0, Math.min(100, value == null ? 50 : value))}%`
}

async function refresh() {
  loading.value = true
  err.value = ''
  const result = await signalApi.latest(showAll.value)
  loading.value = false
  if (isError(result)) {
    err.value = result.error
    return
  }
  report.value = result
}

onMounted(refresh)
defineExpose({ refresh })

async function toggleAll() {
  showAll.value = !showAll.value
  await refresh()
}
</script>

<template>
  <div v-if="loading && !report" class="empty"><span class="spin"></span> 加载策略评分…</div>
  <div v-else-if="err" class="empty num down">加载失败：{{ err }}</div>
  <div v-else-if="!report || report.status === 'none'" class="empty">尚无策略扫描批次</div>
  <div v-else-if="!report.rows.length" class="empty">
    最近批次 {{ report.signal_date }} 已完成，但尚未在「设置 → 策略评分」选择接收股票。
    <button class="btn ghost sm" @click="toggleAll">查看全部 {{ report.universe_size }} 只评分</button>
  </div>
  <div v-else class="signals">
    <div class="scan-meta">
      <b>{{ report.signal_date }} 策略评分</b>
      <span>因子 {{ report.factor_completed }}/{{ report.factor_expected }}</span>
      <span>LLM {{ report.llm_completed }}/{{ report.llm_requested }}</span>
      <button class="btn ghost sm view-toggle" @click="toggleAll">{{ showAll ? '仅看发送股票' : '查看全部成分' }}</button>
    </div>
    <article v-for="row in report.rows" :key="row.code" class="signal-card">
      <header>
        <div><b>{{ row.name || row.code }}</b><small>{{ row.code }}</small></div>
        <span v-if="row.llm_enabled" class="tag">LLM</span>
      </header>
      <div v-if="showAll || row.factor_mail_enabled" class="strategy-list">
        <div v-for="strategy in row.strategies" :key="strategy.strategy_id" class="strategy-row">
          <div class="strategy-title">
            <b>{{ strategy.strategy_name || strategy.strategy_id }}</b>
            <small>{{ strategy.strategy_id }}</small>
          </div>
          <div class="score-track"><i :style="{ width: barWidth(strategy.score) }"></i><em></em></div>
          <strong :class="scoreClass(strategy.score)">{{ scoreText(strategy.score) }}</strong>
        </div>
      </div>
      <div v-if="row.llm_enabled" class="llm-box">
        <template v-if="row.llm?.status === 'success'">
          <div class="llm-title">
            <b>LLM 独立评分</b>
            <strong :class="scoreClass(row.llm.score)">{{ scoreText(row.llm.score) }}</strong>
            <small>{{ row.llm.model }}</small>
          </div>
          <p>{{ row.llm.summary }}</p>
          <ul v-if="row.llm.risks.length"><li v-for="risk in row.llm.risks" :key="risk">{{ risk }}</li></ul>
        </template>
        <span v-else class="down">LLM 本次失败：{{ row.llm?.error || '无结果' }}</span>
      </div>
    </article>
  </div>
</template>

<style scoped>
.scan-meta{display:flex;gap:15px;align-items:center;padding:10px 12px;color:var(--ink-mute);font-size:12px}.scan-meta b{color:var(--ink);font-size:14px}.view-toggle{margin-left:auto}.signal-card{border-top:1px solid var(--line);padding:13px 12px}.signal-card>header{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}.signal-card>header b{font-size:15px}.signal-card>header small{margin-left:7px;color:var(--ink-mute);font-family:var(--mono)}.strategy-row{display:grid;grid-template-columns:minmax(180px,1fr) minmax(140px,2fr) 55px;gap:12px;align-items:center;padding:6px 0}.strategy-title small{display:block;color:var(--ink-mute);font-size:10px}.score-track{height:7px;background:linear-gradient(90deg,#2f8b64 0 40%,#b59b62 40% 60%,#c94942 60%);border-radius:6px;position:relative;overflow:hidden}.score-track i{display:block;height:100%;background:#fff8;border-right:2px solid var(--ink);position:absolute;left:0}.score-track em{position:absolute;left:50%;top:-3px;height:13px;border-left:1px solid #fff}.strategy-row>strong,.llm-title strong{font:700 18px var(--mono);text-align:right}.neutral{color:#9a742e}.llm-box{margin-top:8px;padding:10px 12px;border-left:3px solid #8068aa;background:color-mix(in srgb,#8068aa 8%,transparent);border-radius:5px;font-size:12px}.llm-title{display:flex;gap:10px;align-items:center}.llm-title small{margin-left:auto;color:var(--ink-mute)}.llm-box p{margin:6px 0}.llm-box ul{margin:4px 0;padding-left:18px;color:var(--ink-mute)}
</style>
