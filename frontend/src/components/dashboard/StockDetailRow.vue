<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { indicesApi } from '@/api/modules'
import { isError } from '@/api/client'
import { band, heatArrows } from '@/composables/useGauge'
import type { StockIndexResp } from '@/api/types'

const props = defineProps<{ code: string }>()
const data = ref<StockIndexResp | null>(null)
const err = ref('')
const loading = ref(true)

onMounted(async () => {
  const r = await indicesApi.stock(props.code)
  loading.value = false
  if (isError(r)) {
    err.value = r.error
    return
  }
  data.value = r
})

const factors = computed(() => {
  const c = data.value?.components ?? {}
  return [
    { k: '波动分位', v: c.volatility_pct },
    { k: '动量分位', v: c.momentum_pct },
    { k: '成交分位', v: c.amount_pct },
    { k: 'PE分位', v: c.pe_pct },
    { k: 'PB分位', v: c.pb_pct },
  ]
})
function fmt(v?: number | null): string {
  return v == null ? '—' : (+v).toFixed(2)
}
</script>

<template>
  <div class="detail-inner">
    <div v-if="loading" style="grid-column:1/-1"><span class="spin"></span> 加载个股情绪…</div>
    <div v-else-if="err" style="grid-column:1/-1" class="num down">加载失败:{{ err }}</div>
    <template v-else-if="data">
      <div class="item">
        <div class="k">恐慌 FEAR</div>
        <div class="v" :style="{ color: band(data.fear).color }">
          {{ data.fear == null ? '—' : Math.round(data.fear) }} <small>{{ band(data.fear).label }}</small>
        </div>
      </div>
      <div class="item">
        <div class="k">贪婪 GREED</div>
        <div class="v" :style="{ color: band(data.greed).color }">
          {{ data.greed == null ? '—' : Math.round(data.greed) }} <small>{{ band(data.greed).label }}</small>
        </div>
      </div>
      <div class="item">
        <div class="k">热度 HEAT</div>
        <div class="v">
          <span v-html="heatArrows(data.heat, data.today_chg)"></span>
          <small>分位 {{ data.heat == null ? '' : Math.round(data.heat) }}</small>
        </div>
      </div>
      <div v-for="f in factors" :key="f.k" class="item">
        <div class="k">{{ f.k }}</div>
        <div class="v">{{ fmt(f.v) }}</div>
      </div>
      <div class="item">
        <div class="k">因子数 F/G/H</div>
        <div class="v">
          {{ data.factor_counts?.fear || 0 }}/{{ data.factor_counts?.greed || 0 }}/{{ data.factor_counts?.heat || 0 }}
        </div>
      </div>
    </template>
  </div>
</template>
