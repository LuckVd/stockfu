<script setup lang="ts">
import { ref, computed } from 'vue'
import { sectorsApi } from '@/api/modules'
import { isError } from '@/api/client'
import { signed } from '@/composables/useFormat'
import type { FlowItem } from '@/api/types'

const items = ref<FlowItem[]>([])
const loading = ref(false)
const err = ref('')

async function refresh() {
  loading.value = true
  err.value = ''
  const r = await sectorsApi.flow(90)
  loading.value = false
  if (isError(r)) {
    err.value = r.error
    return
  }
  items.value = r.top || []
}

const total = computed(() => items.value.reduce((a, r) => a + (r.net_inflow || 0), 0))
const tup = computed(() => total.value >= 0)
const show = computed(() => {
  const rows = items.value.slice().sort((a, b) => (b.net_inflow || 0) - (a.net_inflow || 0))
  const top = rows.filter((r) => (r.net_inflow || 0) > 0).slice(0, 20)
  const bot = rows.filter((r) => (r.net_inflow || 0) < 0).slice(-20)
  return [...top, ...bot]
})
const maxNet = computed(() => Math.max(...show.value.map((r) => Math.abs(r.net_inflow || 0)), 1))

function barWidth(v: number) {
  return ((Math.abs(v) / maxNet.value) * 100).toFixed(1)
}

defineExpose({ refresh })
</script>

<template>
  <div v-if="loading && !items.length" class="empty"><span class="spin"></span> 加载中…</div>
  <div v-else-if="err" class="empty num down">加载失败:{{ err }}</div>
  <div v-else-if="!items.length" class="empty">暂无资金流向数据（数据源限流）</div>
  <template v-else>
    <div class="mkt-card" :class="tup ? 'up' : 'down'">
      <span class="mkt-lbl">市场资金流向 · 今日净额</span>
      <span class="mkt-val">{{ signed(total, 1) }}亿</span>
      <span class="mkt-tag">{{ tup ? '净流入' : '净流出' }} · 共 {{ items.length }} 板块</span>
    </div>
    <div class="bars">
      <div v-for="r in show" :key="r.name" class="bar-row">
        <span class="bar-lbl">
          <b>{{ r.name }}</b>
          <em :class="(r.net_inflow || 0) >= 0 ? 'up' : 'down'">{{ signed(r.net_inflow, 1) }}亿</em>
        </span>
        <div class="bar-track">
          <div
            :class="(r.net_inflow || 0) >= 0 ? 'bar-in' : 'bar-out'"
            :style="{ width: barWidth(r.net_inflow || 0) + '%' }"
          ></div>
        </div>
      </div>
    </div>
  </template>
</template>
