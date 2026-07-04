<script setup lang="ts">
import { computed } from 'vue'
import { usePortfolioStore } from '@/stores/portfolio'
import { nf, signed } from '@/composables/useFormat'

const store = usePortfolioStore()
const s = computed(() => store.summary)
</script>

<template>
  <div v-if="store.loading && !s" class="empty"><span class="spin"></span> 加载组合中…</div>
  <template v-else-if="s">
    <section class="summary">
      <div class="cell"><div class="k">市值</div><div class="v gold">{{ nf(s.total_value) }}</div></div>
      <div class="cell"><div class="k">成本</div><div class="v dim">{{ nf(s.total_cost) }}</div></div>
      <div class="cell">
        <div class="k">盈亏</div>
        <div class="v" :class="(s.total_profit || 0) >= 0 ? 'up' : 'down'">{{ signed(s.total_profit) }}</div>
      </div>
      <div class="cell">
        <div class="k">整体股息率</div>
        <div class="v gold">{{ (s.blended_yield_pct ?? 0).toFixed(2) }}<small>%</small></div>
      </div>
      <div class="cell"><div class="k">年红利 ≈</div><div class="v">{{ nf(s.annual_dividend_income) }}</div></div>
    </section>
    <div v-if="s.as_of" class="summary-foot">截至 {{ s.as_of }}</div>
  </template>
</template>
