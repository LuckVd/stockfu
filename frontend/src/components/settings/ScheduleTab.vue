<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useMessage } from 'naive-ui'
import { configApi } from '@/api/modules'
import { isError } from '@/api/client'

const message = useMessage()
const loading = ref(true)
const saving = ref(false)
const time = ref('')
const interval = ref('')
const count = ref('')

onMounted(load)
async function load() {
  loading.value = true
  const r = await configApi.getSchedule()
  loading.value = false
  if (isError(r)) {
    message.error('读取失败：' + r.error)
    return
  }
  time.value = r.daily_fetch_time || ''
  interval.value = r.fetch_retry_interval != null ? String(r.fetch_retry_interval) : ''
  count.value = r.fetch_retry_count != null ? String(r.fetch_retry_count) : ''
}
async function save() {
  const b: Record<string, number | string> = {}
  if (time.value.trim()) b.daily_fetch_time = time.value.trim()
  if (interval.value.trim()) b.fetch_retry_interval = Number(interval.value.trim())
  if (count.value.trim()) b.fetch_retry_count = Number(count.value.trim())
  saving.value = true
  const r = await configApi.setSchedule(b)
  saving.value = false
  if (isError(r)) {
    message.error('保存失败：' + r.error)
    return
  }
  message.success('定时配置已保存')
}
</script>

<template>
  <div class="set-group">
    <div class="set-gtitle">定时抓取（北京时间）</div>
    <div class="desc">工作日每天到点自动抓当日收盘行情 + 算指数（失败按间隔重试 N 次）。盘中 / 未到点显示前一交易日。</div>
    <div v-if="loading" class="lab"><span class="spin"></span>加载中…</div>
    <template v-else>
      <div class="lab">抓取时间 HH:MM</div>
      <input v-model="time" placeholder="15:30">
      <div class="lab">失败重试间隔（分钟）</div>
      <input v-model="interval" inputmode="numeric" placeholder="10">
      <div class="lab">重试次数</div>
      <input v-model="count" inputmode="numeric" placeholder="3">
      <div class="actions">
        <button class="btn" :disabled="saving" @click="save">保存</button>
      </div>
    </template>
  </div>
</template>
