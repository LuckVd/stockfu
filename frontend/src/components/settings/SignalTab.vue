<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useDialog, useMessage } from 'naive-ui'
import { signalApi } from '@/api/modules'
import { isError } from '@/api/client'
import type { SignalConfig, SignalSubscription } from '@/api/types'

const message = useMessage()
const dialog = useDialog()
const loading = ref(true)
const saving = ref(false)
const testing = ref(false)
const cfg = ref<SignalConfig | null>(null)
const subscriptions = ref<SignalSubscription[]>([])
const query = ref('')
const filter = ref<'all' | 'factor' | 'llm'>('all')
const page = ref(1)
const pageSize = 30

const factorEnabled = ref(true)
const llmEnabled = ref(false)
const mailEnabled = ref(false)
const scanTime = ref('16:10')
const strategyIds = ref<string[]>([])

const filtered = computed(() => {
  const q = query.value.trim().toLowerCase()
  return subscriptions.value.filter((row) => {
    if (filter.value === 'factor' && !row.factor_mail_enabled) return false
    if (filter.value === 'llm' && !row.llm_enabled) return false
    return !q || row.code.toLowerCase().includes(q) || (row.name || '').toLowerCase().includes(q)
  })
})
const pages = computed(() => Math.max(1, Math.ceil(filtered.value.length / pageSize)))
const visible = computed(() => filtered.value.slice((page.value - 1) * pageSize, page.value * pageSize))

watch([query, filter], () => { page.value = 1 })
watch(pages, (n) => { if (page.value > n) page.value = n })

onMounted(load)
async function load() {
  loading.value = true
  const [configResult, subscriptionResult] = await Promise.all([
    signalApi.getConfig(), signalApi.subscriptions(),
  ])
  loading.value = false
  if (isError(configResult)) {
    message.error('读取策略扫描配置失败：' + configResult.error)
    return
  }
  cfg.value = configResult
  factorEnabled.value = configResult.factor_enabled
  llmEnabled.value = configResult.llm_enabled
  mailEnabled.value = configResult.mail_enabled
  scanTime.value = configResult.scan_time
  strategyIds.value = [...configResult.strategy_ids]
  if (isError(subscriptionResult)) {
    message.error('读取股票订阅失败：' + subscriptionResult.error)
    return
  }
  subscriptions.value = subscriptionResult.rows
}

async function saveConfig() {
  if (!strategyIds.value.length) {
    message.warning('至少选择一个策略')
    return
  }
  saving.value = true
  const result = await signalApi.setConfig({
    factor_enabled: factorEnabled.value,
    llm_enabled: llmEnabled.value,
    mail_enabled: mailEnabled.value,
    scan_time: scanTime.value.trim() || '16:10',
    strategy_ids: strategyIds.value,
  })
  saving.value = false
  if (isError(result)) {
    message.error('保存失败：' + result.error)
    return
  }
  cfg.value = result
  message.success('策略扫描配置已保存；调度时间变更需重启 --schedule')
}

async function toggle(row: SignalSubscription, field: 'factor_mail_enabled' | 'llm_enabled') {
  const result = await signalApi.setSubscriptions([{ code: row.code, [field]: row[field] }])
  if (isError(result)) {
    row[field] = !row[field]
    message.error('保存失败：' + result.error)
  }
}

async function bulk(field: 'factor_mail_enabled' | 'llm_enabled', value: boolean) {
  const targets = filtered.value
  if (!targets.length) return
  if (field === 'llm_enabled' && value) {
    dialog.warning({
      title: '批量开启 LLM',
      content: `将为当前筛选的 ${targets.length} 只股票每天调用 LLM，可能产生较多耗时和费用。确认开启？`,
      positiveText: '确认开启',
      negativeText: '取消',
      onPositiveClick: () => applyBulk(field, value, targets),
    })
    return
  }
  await applyBulk(field, value, targets)
}

async function applyBulk(
  field: 'factor_mail_enabled' | 'llm_enabled',
  value: boolean,
  targets: SignalSubscription[],
) {
  const result = await signalApi.setSubscriptions(targets.map((row) => ({ code: row.code, [field]: value })))
  if (isError(result)) {
    message.error('批量保存失败：' + result.error)
    return
  }
  targets.forEach((row) => { row[field] = value })
  message.success(`已更新 ${result.updated} 只股票`)
}

async function testMail() {
  testing.value = true
  const result = await signalApi.testMail()
  testing.value = false
  if (isError(result)) {
    message.error('测试失败：' + result.error)
  } else if (!result.ok) {
    message.error(result.detail)
  } else {
    message.success(`已发送 ${result.pages || 0} 张推荐卡片`)
  }
}
</script>

<template>
  <div class="signal-settings">
    <div v-if="loading" class="empty"><span class="spin"></span>加载策略和指数成分…</div>
    <template v-else-if="cfg">
      <div class="config-grid">
        <div>
          <div class="set-gtitle">每日扫描</div>
          <label class="chk"><input v-model="factorEnabled" type="checkbox"> 全量因子扫描并落库</label>
          <label class="chk"><input v-model="llmEnabled" type="checkbox"> 允许逐股 LLM 分析</label>
          <label class="chk"><input v-model="mailEnabled" type="checkbox"> 发送策略推荐邮件</label>
          <div class="lab">扫描时间（北京时间）</div>
          <input v-model="scanTime" placeholder="16:10">
        </div>
        <div>
          <div class="set-gtitle">启用策略（多选）</div>
          <select v-model="strategyIds" multiple size="8" class="strategy-select">
            <option v-for="item in cfg.available_strategies" :key="item.strategy_id" :value="item.strategy_id">
              {{ item.name }} · {{ item.strategy_id }}
            </option>
          </select>
          <div class="hint">各策略每天独立输出 0–100 分，50 为中性。</div>
        </div>
      </div>
      <div class="actions config-actions">
        <button class="btn ghost" :disabled="testing" @click="testMail">发送最近批次测试邮件</button>
        <button class="btn" :disabled="saving" @click="saveConfig">保存扫描配置</button>
      </div>

      <div class="sub-head">
        <div>
          <div class="set-gtitle">逐股发送与 LLM 开关</div>
          <div class="hint">因子仍对全部成分落库；“因子邮件”只控制发送。LLM 只分析勾选股票。</div>
        </div>
        <div class="filters">
          <input v-model="query" placeholder="搜索代码或名称">
          <select v-model="filter">
            <option value="all">全部成分</option>
            <option value="factor">已开因子邮件</option>
            <option value="llm">已开 LLM</option>
          </select>
        </div>
      </div>
      <div class="bulk-row">
        <span>当前筛选 {{ filtered.length }} 只</span>
        <button class="btn ghost sm" @click="bulk('factor_mail_enabled', true)">因子全开</button>
        <button class="btn ghost sm" @click="bulk('factor_mail_enabled', false)">因子全关</button>
        <button class="btn ghost sm" @click="bulk('llm_enabled', true)">LLM 全开</button>
        <button class="btn ghost sm" @click="bulk('llm_enabled', false)">LLM 全关</button>
      </div>
      <div class="subscription-wrap">
        <table class="subscription-table">
          <thead><tr><th>股票</th><th>指数</th><th>因子邮件</th><th>LLM</th></tr></thead>
          <tbody>
            <tr v-for="row in visible" :key="row.code">
              <td><b>{{ row.name || row.code }}</b><small>{{ row.code }}</small></td>
              <td>{{ row.index_codes.map((v) => v === '000300' ? '沪深300' : '中证500').join(' / ') }}</td>
              <td><input v-model="row.factor_mail_enabled" type="checkbox" @change="toggle(row, 'factor_mail_enabled')"></td>
              <td><input v-model="row.llm_enabled" type="checkbox" @change="toggle(row, 'llm_enabled')"></td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="pager">
        <button class="pg-btn" :disabled="page <= 1" @click="page--">‹ 上一页</button>
        <span>第 {{ page }} / {{ pages }} 页</span>
        <button class="pg-btn" :disabled="page >= pages" @click="page++">下一页 ›</button>
      </div>
    </template>
  </div>
</template>

<style scoped>
.signal-settings{min-height:360px}.config-grid{display:grid;grid-template-columns:1fr 1.45fr;gap:22px}.chk{display:block;margin:10px 0}.strategy-select{width:100%;min-height:178px}.hint{font-size:12px;color:var(--ink-mute);margin-top:5px}.config-actions{display:flex;justify-content:flex-end;gap:8px;border-bottom:1px solid var(--line);padding-bottom:16px}.sub-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-end;margin-top:15px}.filters{display:flex;gap:7px}.filters input{width:170px}.bulk-row{display:flex;align-items:center;gap:7px;margin:12px 0;font-size:12px;color:var(--ink-mute)}.bulk-row span{margin-right:auto}.subscription-wrap{max-height:400px;overflow:auto;border:1px solid var(--line);border-radius:8px}.subscription-table{width:100%;border-collapse:collapse}.subscription-table th,.subscription-table td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;font-size:12px}.subscription-table th:nth-child(n+3),.subscription-table td:nth-child(n+3){text-align:center;width:90px}.subscription-table td small{display:block;color:var(--ink-mute);font-family:var(--mono)}.pager{margin-top:10px}@media(max-width:700px){.config-grid{grid-template-columns:1fr}.sub-head{display:block}.filters{margin-top:8px}.bulk-row{flex-wrap:wrap}.bulk-row span{width:100%}}
</style>
