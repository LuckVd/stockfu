<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useMessage } from 'naive-ui'
import { configApi } from '@/api/modules'
import { isError } from '@/api/client'

const message = useMessage()
const loading = ref(true)
const saving = ref(false)
const testing = ref(false)
const result = ref<{ cls: string; text: string }>({ cls: '', text: '' })

const enabled = ref(false)
const preset = ref('')
const host = ref('')
const port = ref('')
const user = ref('')
const pass = ref('')
const to = ref('')
const time = ref('')
const days = ref('mon-fri')
const hasPass = ref(false)

const PRESET_OPTS = [
  { key: 'qq', label: 'QQ 邮箱' },
  { key: '163', label: '163 邮箱' },
  { key: 'gmail', label: 'Gmail' },
  { key: '', label: '通用 SMTP（自填）' },
]
const PRESET_DEFAULTS: Record<string, { host: string; port: string }> = {
  qq: { host: 'smtp.qq.com', port: '465' },
  '163': { host: 'smtp.163.com', port: '465' },
  gmail: { host: 'smtp.gmail.com', port: '587' },
}

onMounted(load)
async function load() {
  loading.value = true
  const r = await configApi.getMail()
  loading.value = false
  if (isError(r)) {
    message.error('读取失败：' + r.error)
    return
  }
  enabled.value = r.mail_enabled
  host.value = r.smtp_host || ''
  port.value = r.smtp_port ? String(r.smtp_port) : ''
  user.value = r.smtp_user || ''
  to.value = r.mail_to || ''
  time.value = r.mail_time || '16:00'
  days.value = r.mail_days || 'mon-fri'
  hasPass.value = r.has_password
  pass.value = ''
  preset.value = ''
}
function applyPreset() {
  const d = PRESET_DEFAULTS[preset.value]
  if (d) {
    host.value = d.host
    port.value = d.port
  }
}
async function save() {
  const b: Record<string, string | number | boolean> = {
    smtp_host: host.value.trim(),
    smtp_port: Number(port.value.trim()) || 465,
    smtp_user: user.value.trim(),
    mail_to: to.value.trim(),
    mail_enabled: enabled.value,
    mail_time: time.value.trim() || '16:00',
    mail_days: days.value,
  }
  if (pass.value) b.smtp_pass = pass.value
  saving.value = true
  const r = await configApi.setMail(b)
  saving.value = false
  if (isError(r)) {
    message.error('保存失败：' + r.error)
    return
  }
  message.success('邮件配置已保存')
  hasPass.value = r.has_password
  pass.value = ''
}
async function test() {
  testing.value = true
  result.value = { cls: 'wait', text: '测试发送中…（需 --serve 在跑）' }
  const r = await configApi.testMail()
  testing.value = false
  if (isError(r)) {
    result.value = { cls: 'err', text: '失败：' + r.error }
    return
  }
  result.value = r.ok
    ? { cls: 'ok', text: `✓ 已发送 ${r.pages || 0} 图 → ${(r.to || []).join(',')}` }
    : { cls: 'err', text: '✗ ' + r.detail }
}
</script>

<template>
  <div class="set-group">
    <div class="set-gtitle">邮件定时</div>
    <div v-if="loading" class="lab"><span class="spin"></span>加载中…</div>
    <template v-else>
      <label class="chk"><input type="checkbox" v-model="enabled"> 启用定时邮件</label>
      <div class="lab">邮箱服务</div>
      <select v-model="preset" @change="applyPreset">
        <option v-for="o in PRESET_OPTS" :key="o.key" :value="o.key">{{ o.label }}</option>
      </select>
      <div class="lab">SMTP 主机</div>
      <input v-model="host" placeholder="smtp.qq.com">
      <div class="lab">SMTP 端口</div>
      <input v-model="port" inputmode="numeric" placeholder="465">
      <div class="lab">发件账号</div>
      <input v-model="user" placeholder="you@qq.com">
      <div class="lab">授权码（非登录密码；留空保存 = 不改{{ hasPass ? '，已设置' : '' }}）</div>
      <input v-model="pass" type="password" placeholder="邮箱授权码">
      <div class="lab">收件人（多个用逗号）</div>
      <input v-model="to" placeholder="friend@example.com">
      <div class="lab">发送时间 HH:MM</div>
      <input v-model="time" placeholder="16:00">
      <div class="lab">频率</div>
      <select v-model="days">
        <option value="mon-fri">工作日（周一至周五）</option>
        <option value="*">每天</option>
      </select>
      <div class="row">
        <button class="btn ghost sm" :disabled="testing" @click="test">测试发送</button>
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
