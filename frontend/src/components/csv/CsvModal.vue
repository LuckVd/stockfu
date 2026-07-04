<script setup lang="ts">
import { ref, watch } from 'vue'
import { NModal, useMessage } from 'naive-ui'
import { csvApi } from '@/api/modules'
import { isError } from '@/api/client'
import { usePortfolioStore } from '@/stores/portfolio'
import { useWatchlistStore } from '@/stores/watchlist'

const props = defineProps<{ show: boolean; mode: 'import' | 'export' }>()
const emit = defineEmits<{ 'update:show': [boolean] }>()
const message = useMessage()
const portfolio = usePortfolioStore()
const watchlist = useWatchlistStore()

type Scope = 'holdings' | 'watchlist'
const scope = ref<Scope>('holdings')
const file = ref<File | null>(null)
const result = ref<{ cls: string; text: string }>({ cls: '', text: '' })
const busy = ref(false)

const SCOPE_LABEL: Record<Scope, string> = { holdings: '持仓', watchlist: '自选' }
const CSV_HEAD: Record<Scope, { name: string; req: boolean }[]> = {
  holdings: [
    { name: '代码', req: true }, { name: '方向', req: true }, { name: '股数', req: true },
    { name: '价格', req: true }, { name: '日期', req: false }, { name: '备注', req: false },
  ],
  watchlist: [
    { name: '代码', req: true }, { name: '名称', req: false }, { name: '市场', req: false },
    { name: '类型', req: false }, { name: '板块', req: false }, { name: '币种', req: false },
    { name: '自选', req: false }, { name: '备注', req: false },
  ],
}
const CSV_HINT: Record<Scope, string> = {
  holdings: '<b>方向</b>填 buy / sell / dividend（或 买入/卖出/分红）；<b>日期</b> YYYY-MM-DD。',
  watchlist: '<b>市场</b> cn / hk / us（选填）；<b>自选</b> 1 或 0（选填）。',
}

function reset() {
  file.value = null
  result.value = { cls: '', text: '' }
}
watch(() => props.show, (v) => { if (v) reset() })
watch(scope, () => reset())

function onFile(e: Event) {
  const t = e.target as HTMLInputElement
  file.value = t.files && t.files[0] ? t.files[0] : null
  result.value = { cls: '', text: '' }
}
async function doImport() {
  if (!file.value) {
    result.value = { cls: 'err', text: '请先选择 CSV 文件' }
    return
  }
  busy.value = true
  result.value = { cls: 'wait', text: '导入中…' }
  const r = await csvApi.importScope(scope.value, file.value)
  busy.value = false
  if (isError(r)) {
    result.value = { cls: 'err', text: '✗ ' + r.error }
    return
  }
  const c = r.counts || {}
  result.value = { cls: 'ok', text: `✓ +${c.inserted ?? 0} 新增  ~${c.updated ?? 0} 更新  =${c.skipped ?? 0} 跳过` }
  message.success('CSV 导入完成，刷新中')
  if (scope.value === 'holdings') await portfolio.fetch()
  else await watchlist.fetch()
}
async function doExport() {
  busy.value = true
  result.value = { cls: 'wait', text: '导出中…' }
  const r = await csvApi.exportScope(scope.value)
  busy.value = false
  if (isError(r)) {
    result.value = { cls: 'err', text: '✗ 导出失败 ' + r.error }
    return
  }
  result.value = { cls: 'ok', text: `✓ 已下载 ${r.filename}（${r.rows} 行）` }
  message.success(`已导出${SCOPE_LABEL[scope.value]} ${r.rows} 行`)
}
</script>

<template>
  <NModal
    :show="show"
    preset="card"
    :title="mode === 'import' ? '导入 CSV' : '导出 CSV'"
    :bordered="false"
    style="max-width: 560px"
    :body-style="{ padding: '8px 24px 20px' }"
    @update:show="(v: boolean) => emit('update:show', v)"
  >
    <div class="lab">数据类型</div>
    <div class="csv-seg">
      <button :class="{ on: scope === 'holdings' }" @click="scope = 'holdings'">持仓</button>
      <button :class="{ on: scope === 'watchlist' }" @click="scope = 'watchlist'">自选</button>
    </div>

    <template v-if="mode === 'import'">
      <div class="lab">表头<span class="lab-hint">（标 * 为必填）</span></div>
      <div class="csv-head">
        <span v-for="h in CSV_HEAD[scope]" :key="h.name" class="ch" :class="{ req: h.req }">{{ h.name }}</span>
      </div>
      <div class="csv-hint" v-html="CSV_HINT[scope]"></div>
      <div class="row">
        <a class="btn ghost sm" :href="csvApi.templateUrl(scope)" download>下载模板文件</a>
      </div>
      <div class="lab">选择 CSV 文件</div>
      <label class="file-pick">
        <span class="file-pick-btn">选择文件</span>
        <span class="file-name">{{ file ? file.name : '未选择文件' }}</span>
        <input type="file" accept=".csv,text/csv" hidden @change="onFile">
      </label>
    </template>

    <div class="test-result" :class="result.cls">
      <span v-if="result.cls === 'wait'" class="spin"></span>{{ result.text }}
    </div>

    <div class="actions">
      <button class="btn ghost" @click="emit('update:show', false)">关闭</button>
      <button v-if="mode === 'import'" class="btn" :disabled="busy" @click="doImport">导入</button>
      <button v-else class="btn" :disabled="busy" @click="doExport">导出下载</button>
    </div>
  </NModal>
</template>
