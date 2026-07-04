<script setup lang="ts">
import { NModal } from 'naive-ui'
import { useThemeStore, THEMES } from '@/stores/theme'

defineProps<{ show: boolean }>()
const emit = defineEmits<{ 'update:show': [boolean] }>()
const theme = useThemeStore()

function pick(key: string) {
  theme.setTheme(key)
  emit('update:show', false)
}
</script>

<template>
  <NModal :show="show" preset="card" title="主题配色" :bordered="false" style="max-width: 460px"
          @update:show="(v: boolean) => emit('update:show', v)">
    <div class="desc">挑一套配色;选择会记住,刷新后保持。</div>
    <div class="grid">
      <button v-for="t in THEMES" :key="t.key" class="opt" :class="{ on: t.key === theme.key }" @click="pick(t.key)">
        <span class="sw" :style="{ background: t.swatch }"><i :style="{ background: t.ink }"></i></span>
        {{ t.label }}
      </button>
    </div>
  </NModal>
</template>

<style scoped>
.desc { font-size: 13px; color: var(--ink-mute); margin: 8px 0 14px; }
.grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; }
.opt { display: flex; align-items: center; gap: 10px; padding: 10px 12px; border: 1px solid var(--line); border-radius: 10px; background: var(--surface); cursor: pointer; font-family: var(--sans); font-size: 13px; color: var(--ink); transition: .15s; }
.opt:hover { border-color: var(--gold); }
.opt.on { border-color: var(--gold); background: var(--gold-glow); font-weight: 600; }
.sw { width: 22px; height: 22px; border-radius: 6px; border: 1px solid var(--line); display: inline-flex; align-items: center; justify-content: center; flex-shrink: 0; }
.sw i { width: 10px; height: 10px; border-radius: 50%; }
</style>
