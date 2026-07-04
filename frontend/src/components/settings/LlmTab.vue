<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useMessage } from 'naive-ui'
import { configApi } from '@/api/modules'
import { isError } from '@/api/client'
import type { LlmConfig } from '@/api/types'

const message = useMessage()
const loading = ref(true)
const saving = ref(false)
const testing = ref(false)
const result = ref<{ cls: string; text: string }>({ cls: '', text: '' })
const cfg = ref<LlmConfig | null>(null)
const baseUrl = ref('')
const apiKey = ref('')
const model = ref('')

onMounted(load)
async function load() {
  loading.value = true
  const r = await configApi.getLlm()
  loading.value = false
  if (isError(r)) {
    message.error('读取失败：' + r.error)
    return
  }
  cfg.value = r
  baseUrl.value = r.llm_base_url || ''
  model.value = r.llm_model || ''
  apiKey.value = '' // 脱敏，不回填
}
async function save(): Promise<boolean> {
  const b: Record<string, string> = {
    llm_base_url: baseUrl.value.trim(),
    llm_model: model.value.trim(),
  }
  if (apiKey.value.trim()) b.llm_api_key = apiKey.value.trim()
  saving.value = true
  const r = await configApi.setLlm(b)
  saving.value = false
  if (isError(r)) {
    message.error('保存失败：' + r.error)
    return false
  }
  message.success('AI 模型配置已保存')
  await load()
  return true
}
async function test() {
  // 先存（否则测的是旧 base_url），再测
  const ok = await save()
  if (!ok) return
  testing.value = true
  result.value = { cls: 'wait', text: '测试中…' }
  const r = await configApi.testLlm()
  testing.value = false
  if (isError(r)) {
    result.value = { cls: 'err', text: '失败：' + r.error }
    return
  }
  result.value = r.ok
    ? { cls: 'ok', text: `✓ ${r.detail}${r.reply ? '：' + r.reply : ''}` }
    : { cls: 'err', text: '✗ ' + r.detail }
}
</script>

<template>
  <div class="set-group">
    <div class="set-gtitle">AI 模型</div>
    <div v-if="loading" class="lab"><span class="spin"></span>加载中…</div>
    <template v-else>
      <div v-if="cfg" class="cur">
        当前：<b>{{ cfg.llm_model || '—' }}</b> @ {{ cfg.llm_base_url || '—' }}
        <span class="src">{{ cfg.has_api_key ? '（已设 Key）' : '（未设 Key）' }}</span>
      </div>
      <div class="lab">API 地址（网关）</div>
      <input v-model="baseUrl" placeholder="https://api.openai.com/v1">
      <div class="lab">API Key（留空保存 = 不改）</div>
      <input v-model="apiKey" type="password" placeholder="sk-...">
      <div class="lab">模型</div>
      <input v-model="model" placeholder="deepseek-v4-flash">
      <div class="row">
        <button class="btn ghost sm" :disabled="testing" @click="test">测试连接</button>
        <span class="test-result" :class="result.cls">
          <span v-if="result.cls === 'wait'" class="spin"></span>{{ result.text }}
        </span>
      </div>
      <div class="actions">
        <button class="btn" :disabled="saving" @click="save">保存</button>
      </div>
    </template>
  </div>
</template>
