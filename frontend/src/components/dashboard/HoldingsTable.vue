<script setup lang="ts">
import { ref } from 'vue'
import { useDialog, useMessage } from 'naive-ui'
import { usePortfolioStore } from '@/stores/portfolio'
import { indexBar } from '@/composables/useGauge'
import { nf, pct } from '@/composables/useFormat'
import type { PositionView } from '@/api/types'
import StockDetailRow from './StockDetailRow.vue'
import AiButton from '@/components/ai/AiButton.vue'
import AiReportModal from '@/components/ai/AiReportModal.vue'

const store = usePortfolioStore()
const dialog = useDialog()
const message = useMessage()

const showAi = ref(false)
const curAiCode = ref('')
function onViewAi(code: string) {
  curAiCode.value = code
  showAi.value = true
}

const expanded = ref<string[]>([])
function toggle(code: string) {
  expanded.value = expanded.value.includes(code)
    ? expanded.value.filter((c) => c !== code)
    : [...expanded.value, code]
}
function isExpanded(code: string) {
  return expanded.value.includes(code)
}

function sortClass(key: string): string {
  if (store.holdSort.key !== key) return ''
  return store.holdSort.dir === 'asc' ? 'sort-asc' : 'sort-desc'
}

function hasPrice(p: PositionView) {
  return p.price != null && p.price > 0
}
function yld(v: number | null) {
  return v == null ? '—' : v.toFixed(2)
}
function pbk(v: number | null) {
  return v == null ? '—' : v.toFixed(1)
}

function onDelete(p: PositionView) {
  dialog.warning({
    title: '确认删除',
    content: `确认删除 ${p.code}（${p.name || ''}）的持仓？会清除该股票的全部交易流水，自选保留。`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        const r = await store.removeHolding(p.code)
        message.success(`已删除 ${p.code}（${r.deleted_transactions} 笔交易）`)
      } catch (e: any) {
        message.error('删除失败：' + (e?.message || e))
      }
    },
  })
}
</script>

<template>
  <div class="tbl-wrap">
    <table class="ht-tbl">
      <thead>
        <tr>
          <th data-sort="code" :class="sortClass('code')" @click="store.setSort('code')">代码</th>
          <th data-sort="name" :class="sortClass('name')" @click="store.setSort('name')">名称</th>
          <th data-sort="shares" :class="sortClass('shares')" @click="store.setSort('shares')">持仓</th>
          <th data-sort="avg_cost" :class="sortClass('avg_cost')" @click="store.setSort('avg_cost')">成本</th>
          <th data-sort="price" :class="sortClass('price')" @click="store.setSort('price')">现价</th>
          <th data-sort="market_value" :class="sortClass('market_value')" @click="store.setSort('market_value')">市值</th>
          <th data-sort="profit_pct" :class="sortClass('profit_pct')" @click="store.setSort('profit_pct')">盈亏%</th>
          <th data-sort="ttm_yield_pct" :class="sortClass('ttm_yield_pct')" @click="store.setSort('ttm_yield_pct')">股息率%</th>
          <th data-sort="annual_dividend" :class="sortClass('annual_dividend')" @click="store.setSort('annual_dividend')">年红利</th>
          <th data-sort="payback_years" :class="sortClass('payback_years')" @click="store.setSort('payback_years')">回本</th>
          <th data-sort="currency" :class="sortClass('currency')" @click="store.setSort('currency')">币种</th>
          <th data-sort="fear" :class="sortClass('fear')" @click="store.setSort('fear')">恐慌</th>
          <th data-sort="greed" :class="sortClass('greed')" @click="store.setSort('greed')">贪婪</th>
          <th data-sort="heat" :class="sortClass('heat')" @click="store.setSort('heat')">热度</th>
          <th class="op">操作</th>
        </tr>
      </thead>
      <tbody>
        <template v-if="!store.positions.length">
          <tr>
            <td colspan="15" class="empty">
              <span v-if="store.loading"><span class="spin"></span> 加载持仓中…</span>
              <span v-else>暂无持仓 — 在右侧买入第一只</span>
            </td>
          </tr>
        </template>
        <template v-for="p in store.sortedHoldings" :key="p.code" v-else>
          <tr class="rise" :class="{ expanded: isExpanded(p.code) }">
            <td @click="toggle(p.code)">{{ p.code }}</td>
            <td>{{ p.name || p.code }}</td>
            <td>{{ nf(p.shares) }}</td>
            <td class="num mute">{{ nf(p.avg_cost, 2) }}</td>
            <td class="num">{{ hasPrice(p) ? nf(p.price, 2) : '—' }}</td>
            <td class="num">{{ hasPrice(p) ? nf(p.market_value) : '—' }}</td>
            <td class="num" :class="(p.profit_pct || 0) >= 0 ? 'up' : 'down'">
              {{ hasPrice(p) ? pct(p.profit_pct) : '—' }}
            </td>
            <td class="num" style="color: var(--gold-hi)">{{ yld(p.ttm_yield_pct) }}</td>
            <td class="num mute">{{ nf(p.annual_dividend) }}</td>
            <td class="num mute">{{ pbk(p.payback_years) }}</td>
            <td class="num mute">{{ p.currency || '' }}</td>
            <td v-html="indexBar(p.fear, true)"></td>
            <td v-html="indexBar(p.greed, true)"></td>
            <td v-html="indexBar(p.heat, true, p.day_chg)"></td>
            <td class="op">
              <AiButton :code="p.code" :name="p.name" @view="onViewAi" />
              <button class="row-del" title="删除持仓" @click.stop="onDelete(p)">×</button>
            </td>
          </tr>
          <tr v-if="isExpanded(p.code)" class="detail">
            <td colspan="15"><StockDetailRow :code="p.code" /></td>
          </tr>
        </template>
      </tbody>
    </table>
    <AiReportModal v-model:show="showAi" :code="curAiCode" />
  </div>
</template>

<style scoped></style>
