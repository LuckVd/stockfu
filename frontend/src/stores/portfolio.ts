import { defineStore } from 'pinia'
import { portfolioApi, holdingApi, aiApi } from '@/api/modules'
import { isError } from '@/api/client'
import type { PortfolioResp, PositionView, DelHoldingResp, AiSignal } from '@/api/types'
import { sortBy, nextSort, type SortState } from '@/composables/useSort'

interface AiState {
  status: 'none' | 'pending' | 'done'
  signal?: AiSignal | null
  analyzed_at?: string
}

// 组合汇总 + 持仓列表共享一次 /portfolio（Summary 与 HoldingsTable 共用）。
// aiStates 缓存各股 AI 分析状态,AiButton 读它初始化/上色,loadAiResults 批量恢复。
export const usePortfolioStore = defineStore('portfolio', {
  state: () => ({
    data: null as PortfolioResp | null,
    err: '' as string,
    loading: false,
    holdSort: { key: 'market_value', dir: 'desc' } as SortState,
    aiStates: {} as Record<string, AiState>,
    aiLoaded: false,
  }),
  getters: {
    positions: (state): PositionView[] => state.data?.positions ?? [],
    summary: (state): PortfolioResp | null => state.data,
    sortedHoldings(state): PositionView[] {
      return sortBy(state.data?.positions ?? [], state.holdSort.key, state.holdSort.dir)
    },
  },
  actions: {
    async fetch() {
      this.loading = true
      this.err = ''
      const r = await portfolioApi.list()
      this.loading = false
      if (isError(r)) {
        this.err = r.error
        return
      }
      this.data = r
      // 持仓就绪后批量恢复各股 AI 状态(只跑一次;之前放在 HoldingsTable.onMounted
      // 时 portfolio 还没 fetch → 空列表直接 return,导致刷新后 AiButton 丢状态)
      if (!this.aiLoaded && r.positions?.length) {
        this.aiLoaded = true
        this.loadAiResults()
      }
    },
    setSort(key: string) {
      this.holdSort = nextSort(this.holdSort, key)
    },
    async removeHolding(code: string): Promise<DelHoldingResp> {
      const r = await holdingApi.del(code)
      if (isError(r)) throw new Error(r.error)
      await this.fetch()
      return r
    },
    setAiState(code: string, st: AiState) {
      this.aiStates[code] = st
    },
    // 持仓加载后批量恢复各股 AI 状态(80ms 错峰,避免瞬时 N 并发打爆后端)
    async loadAiResults() {
      const codes = (this.data?.positions || []).map((p) => p.code)
      if (!codes.length) return
      for (let i = 0; i < codes.length; i++) {
        const c = codes[i]
        window.setTimeout(async () => {
          const r = await aiApi.result(c)
          if (isError(r)) return
          this.aiStates[c] = {
            status: r.status as AiState['status'],
            signal: r.signal,
            analyzed_at: r.analyzed_at,
          }
        }, i * 80)
      }
    },
  },
})
