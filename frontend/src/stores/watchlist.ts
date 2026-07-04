import { defineStore } from 'pinia'
import { watchlistApi, watchApi } from '@/api/modules'
import { isError } from '@/api/client'
import type { WatchlistItem, DelWatchResp } from '@/api/types'
import { sortBy, nextSort, type SortState } from '@/composables/useSort'

const WATCH_PS = 15

// 自选列表共享 store（WatchlistTable 读 + TradePanel 追踪 / CsvModal 导入后刷新）。
export const useWatchlistStore = defineStore('watchlist', {
  state: () => ({
    items: [] as WatchlistItem[],
    loading: false,
    err: '' as string,
    watchSort: { key: null, dir: 'desc' } as SortState,
    watchPage: 1,
  }),
  getters: {
    sorted(state): WatchlistItem[] {
      return sortBy(state.items, state.watchSort.key, state.watchSort.dir)
    },
    pages(state): number {
      return Math.max(1, Math.ceil(state.items.length / WATCH_PS))
    },
    slice(state): WatchlistItem[] {
      const s = sortBy(state.items, state.watchSort.key, state.watchSort.dir)
      return s.slice((state.watchPage - 1) * WATCH_PS, state.watchPage * WATCH_PS)
    },
  },
  actions: {
    async fetch() {
      this.loading = true
      this.err = ''
      const r = await watchlistApi.list()
      this.loading = false
      if (isError(r)) {
        this.err = r.error
        return
      }
      this.items = r
    },
    setSort(key: string) {
      this.watchSort = nextSort(this.watchSort, key)
      this.watchPage = 1
    },
    gotoPage(n: number) {
      if (n < 1 || n > this.pages) return
      this.watchPage = n
    },
    async removeWatch(code: string): Promise<DelWatchResp> {
      const r = await watchApi.del(code)
      if (isError(r)) throw new Error(r.error)
      await this.fetch()
      return r
    },
  },
})
