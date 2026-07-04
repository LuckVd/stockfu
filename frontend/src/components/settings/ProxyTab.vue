<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useMessage } from 'naive-ui'
import { configApi } from '@/api/modules'
import { isError } from '@/api/client'
import type { ProxyConfig } from '@/api/types'

const message = useMessage()
const loading = ref(true)
const saving = ref(false)
const testing = ref(false)
const result = ref<{ cls: string; text: string }>({ cls: '', text: '' })
const cfg = ref<ProxyConfig | null>(null)
const proxyUrl = ref('')

onMounted(load)
async function load() {
  loading.value = true
  const r = await configApi.getProxy()
  loading.value = false
  if (isError(r)) {
    message.error('读取失败：' + r.error)
    return
  }
  cfg.value = r
  proxyUrl.value = r.source === 'db' ? (r.proxy_url || '') : ''
}
async function save() {
  saving.value = true
  const r = await configApi.setProxy({ proxy_url: proxyUrl.value.trim() })
  saving.value = false
  if (isError(r)) {
    message.error('保存失败：' + r.error)
    return
  }
  message.success('代理已保存')
  await load()
}
async function test() {
  testing.value = true
  result.value = { cls: 'wait', text: '测试中…' }
  const r = await configApi.testProxy({ proxy_url: proxyUrl.value.trim() || undefined })
  testing.value = false
  if (isError(r)) {
    result.value = { cls: 'err', text: '失败：' + r.error }
    return
  }
  result.value = r.ok
    ? { cls: 'ok', text: `✓ ${r.detail}（${r.latency_ms ?? '?'}ms）` }
    : { cls: 'err', text: '✗ ' + r.detail }
}
</script>

<template>
  <div class="set-group">
    <div class="set-gtitle">外网代理</div>
    <div v-if="loading" class="lab"><span class="spin"></span>加载中…</div>
    <template v-else>
      <div v-if="cfg" class="cur">
        当前生效：<b>{{ cfg.effective || '直连' }}</b>
        <span class="src">{{ cfg.source === 'db' ? '（已设置）' : '（.env 默认）' }}</span>
      </div>
      <div class="lab">代理地址</div>
      <input v-model="proxyUrl" placeholder="http://127.0.0.1:7890（留空=直连）">
      <div class="row">
        <button class="btn ghost sm" :disabled="testing" @click="test">测试连通性</button>
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
