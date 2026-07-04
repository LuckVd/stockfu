<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import ThemePicker from './ThemePicker.vue'
import SettingsModal from '@/components/settings/SettingsModal.vue'
import CsvModal from '@/components/csv/CsvModal.vue'
import HelpModal from '@/components/help/HelpModal.vue'

const showTheme = ref(false)
const showSettings = ref(false)
const showCsvImport = ref(false)
const showCsvExport = ref(false)
const showHelp = ref(false)
const clock = ref('--:--:--')
let timer = 0

function tick() {
  clock.value = new Date().toLocaleString('zh-CN', { hour12: false }).replace(/\//g, '-')
}
onMounted(() => {
  tick()
  timer = window.setInterval(tick, 1000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <header class="app-header">
    <div class="brand">
      <h1>Stock<span class="gold">Fu</span></h1>
      <div class="sub">资产管理终端 · ASSET &amp; SENTIMENT TERMINAL</div>
    </div>
    <div class="right">
      <span class="clock mono">{{ clock }}</span>
      <button class="btn ghost" @click="showCsvImport = true">导入</button>
      <button class="btn ghost" @click="showCsvExport = true">导出</button>
      <button class="btn ghost" @click="showSettings = true">设置</button>
      <button class="btn ghost" @click="showHelp = true">帮助</button>
      <button class="btn ghost" @click="showTheme = true">主题</button>
    </div>
    <ThemePicker v-model:show="showTheme" />
    <SettingsModal v-model:show="showSettings" />
    <CsvModal v-model:show="showCsvImport" mode="import" />
    <CsvModal v-model:show="showCsvExport" mode="export" />
    <HelpModal v-model:show="showHelp" />
  </header>
</template>

<style scoped>
.app-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  padding: 0 0 20px;
  border-bottom: 1px solid var(--line);
  margin-bottom: 24px;
}
.brand h1 {
  font-family: var(--sans);
  font-weight: 700;
  font-size: 28px;
  line-height: 1.1;
  letter-spacing: -.3px;
  color: var(--ink);
}
.brand h1 .gold { color: var(--gold); }
.brand .sub { margin-top: 6px; font-size: 13px; color: var(--ink-mute); }
.right { display: flex; align-items: center; gap: 10px; font-size: 13px; color: var(--ink-dim); }
.clock { font-variant-numeric: tabular-nums; margin-right: 6px; }
</style>
