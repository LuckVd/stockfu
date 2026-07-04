<script setup lang="ts">
import { useDialog, useMessage } from 'naive-ui'
import { useWatchlistStore } from '@/stores/watchlist'
import { indexBar } from '@/composables/useGauge'
import { nf, pct, curSym } from '@/composables/useFormat'
import type { WatchlistItem } from '@/api/types'

const store = useWatchlistStore()
const dialog = useDialog()
const message = useMessage()

function sortClass(key: string): string {
  if (store.watchSort.key !== key) return ''
  return store.watchSort.dir === 'asc' ? 'sort-asc' : 'sort-desc'
}

function onRemove(w: WatchlistItem) {
  dialog.warning({
    title: '取消追踪',
    content: `取消追踪 ${w.code}（${w.name || ''}）？自选移除；若已持仓则持仓保留。`,
    positiveText: '取消追踪',
    negativeText: '保留',
    onPositiveClick: async () => {
      try {
        await store.removeWatch(w.code)
        message.success(`已取消追踪：${w.code}`)
      } catch (e: any) {
        message.error('取消失败：' + (e?.message || e))
      }
    },
  })
}

function yld(v: number | null) {
  return v == null ? '—' : v.toFixed(2)
}

// 保留给 MainTabs 的懒加载/刷新接口（旧契约）
defineExpose({ refresh: () => store.fetch() })
</script>

<template>
  <div v-if="store.loading && !store.items.length" class="empty"><span class="spin"></span> 加载自选中…</div>
  <div v-else-if="store.err" class="empty num down">加载失败:{{ store.err }}</div>
  <div v-else-if="!store.items.length" class="empty">暂无自选/追踪股 — 在右侧「追踪」添加</div>
  <template v-else>
    <table class="wt-tbl">
      <thead>
        <tr>
          <th data-sort="name" :class="sortClass('name')" @click="store.setSort('name')">名称</th>
          <th data-sort="price" :class="sortClass('price')" @click="store.setSort('price')">现价</th>
          <th data-sort="day_chg" :class="sortClass('day_chg')" @click="store.setSort('day_chg')">涨跌</th>
          <th data-sort="ttm_yield_pct" :class="sortClass('ttm_yield_pct')" @click="store.setSort('ttm_yield_pct')">股息率</th>
          <th data-sort="fear" :class="sortClass('fear')" @click="store.setSort('fear')">恐慌</th>
          <th data-sort="greed" :class="sortClass('greed')" @click="store.setSort('greed')">贪婪</th>
          <th data-sort="heat" :class="sortClass('heat')" @click="store.setSort('heat')">热度</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="w in store.slice" :key="w.code">
          <td>
            {{ w.name || w.code }}<small>{{ w.code }}</small>
            <span v-if="w.is_holding" class="tag">持仓</span>
          </td>
          <td>{{ curSym(w.currency) }}{{ w.price == null ? '—' : nf(w.price, 2) }}</td>
          <td :class="(w.day_chg || 0) >= 0 ? 'num up' : 'num down'">{{ pct(w.day_chg) }}</td>
          <td>{{ yld(w.ttm_yield_pct) }}</td>
          <td v-html="indexBar(w.fear, true)"></td>
          <td v-html="indexBar(w.greed, true)"></td>
          <td v-html="indexBar(w.heat, true, w.day_chg)"></td>
          <td><button class="row-del" title="取消追踪" @click="onRemove(w)">×</button></td>
        </tr>
      </tbody>
    </table>
    <div v-if="store.pages > 1" class="pager">
      <button class="pg-btn" :disabled="store.watchPage <= 1" @click="store.gotoPage(store.watchPage - 1)">‹ 上一页</button>
      <span>第 {{ store.watchPage }} / {{ store.pages }} 页</span>
      <button class="pg-btn" :disabled="store.watchPage >= store.pages" @click="store.gotoPage(store.watchPage + 1)">下一页 ›</button>
    </div>
  </template>
</template>
