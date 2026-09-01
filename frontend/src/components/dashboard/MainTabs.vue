<script setup lang="ts">
import { ref } from 'vue'
import { usePortfolioStore } from '@/stores/portfolio'
import HoldingsTable from './HoldingsTable.vue'
import WatchlistTable from './WatchlistTable.vue'
import FundFlow from './FundFlow.vue'
import TradePanel from './TradePanel.vue'

type Tab = 'holdings' | 'watchlist' | 'sentiment'

const tabs: { key: Tab; label: string }[] = [
  { key: 'holdings', label: '持仓' },
  { key: 'watchlist', label: '自选' },
  { key: 'sentiment', label: '资金流向' },
]

const store = usePortfolioStore()
const curTab = ref<Tab>('holdings')
const refreshing = ref(false)
// holdings 由 App.vue 初始 fetch，标 true 避免切回重复；watchlist/sentiment 懒加载。
const loaded = ref<Record<Tab, boolean>>({ holdings: true, watchlist: false, sentiment: false })

const watchlistRef = ref<{ refresh: () => Promise<void> }>()
const fundflowRef = ref<{ refresh: () => Promise<void> }>()

async function loadTab(t: Tab) {
  if (t === 'holdings') await store.fetch()
  else if (t === 'watchlist') await watchlistRef.value?.refresh()
  else if (t === 'sentiment') await fundflowRef.value?.refresh()
  loaded.value[t] = true
}

function switchTab(t: Tab) {
  curTab.value = t
  if (!loaded.value[t]) loadTab(t)
}

async function onRefresh() {
  refreshing.value = true
  await loadTab(curTab.value)
  setTimeout(() => {
    refreshing.value = false
  }, 700)
}
</script>

<template>
  <div class="main-grid">
    <div class="main-left">
      <div class="main-tabs">
        <button
          v-for="t in tabs"
          :key="t.key"
          :class="{ on: curTab === t.key }"
          @click="switchTab(t.key)"
        >
          {{ t.label }}
        </button>
        <button
          class="tab-refresh"
          :class="{ spinning: refreshing }"
          title="刷新当前列表"
          @click="onRefresh"
        >⟳</button>
      </div>
      <div v-show="curTab === 'holdings'"><HoldingsTable /></div>
      <div v-show="curTab === 'watchlist'"><WatchlistTable ref="watchlistRef" /></div>
      <div v-show="curTab === 'sentiment'"><FundFlow ref="fundflowRef" /></div>
    </div>
    <div class="main-right">
      <TradePanel @switch-tab="switchTab" />
    </div>
  </div>
</template>
