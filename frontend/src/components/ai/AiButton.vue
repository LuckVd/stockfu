<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import { useMessage } from 'naive-ui'
import { usePortfolioStore } from '@/stores/portfolio'
import { aiApi } from '@/api/modules'
import { isError } from '@/api/client'
import { useAiPoll } from '@/composables/useAiPoll'
import type { AiSignal } from '@/api/types'

const props = defineProps<{ code: string; name?: string }>()
const emit = defineEmits<{ view: [code: string] }>()

const store = usePortfolioStore()
const message = useMessage()
const { pollAiResult } = useAiPoll()

const loading = ref(false) // POST 中 或 poll 中
const err = ref('')

const aiState = computed(() => store.aiStates[props.code])
const status = computed<'none' | 'loading' | 'done' | 'err'>(() => {
  if (err.value) return 'err'
  if (loading.value) return 'loading'
  return aiState.value?.status === 'done' ? 'done' : 'none'
})
const signal = computed<AiSignal | null | undefined>(() => aiState.value?.signal)

// 信号 → 主题涨跌色(看多红/看空绿/持有中性棕),跟随主题 var
function sigVar(sig?: AiSignal | null): string {
  if (sig === 'buy' || sig === 'strong_buy') return 'var(--up)'
  if (sig === 'sell' || sig === 'strong_sell') return 'var(--down)'
  return 'var(--neutral)'
}

async function run() {
  loading.value = true
  err.value = ''
  try {
    const res = await Promise.race([
      aiApi.run(props.code),
      new Promise<never>((_, rej) =>
        setTimeout(() => rej(new Error('分析超时(200s)')), 200000),
      ),
    ])
    loading.value = false
    if (isError(res)) {
      err.value = res.error
      return
    }
    store.setAiState(props.code, {
      status: 'done',
      signal: res.aggregate?.final_signal,
      analyzed_at: new Date().toLocaleString('zh-CN', { hour12: false }).slice(0, 16),
    })
    message.success(`${props.name || props.code} 分析完成`)
  } catch (e: any) {
    loading.value = false
    err.value = e?.message || String(e)
  }
}

function startPoll() {
  loading.value = true
  err.value = ''
  pollAiResult(
    props.code,
    (r) => {
      loading.value = false
      store.setAiState(
        props.code,
        r.result
          ? { status: 'done', signal: r.signal, analyzed_at: r.analyzed_at }
          : { status: 'none' },
      )
    },
  )
}

function onClick() {
  if (status.value === 'done') {
    emit('view', props.code)
    return
  }
  if (status.value === 'loading') return
  run()
}

onMounted(() => {
  if (aiState.value?.status === 'pending') startPoll()
})
watch(aiState, (s) => {
  // loadAiResults 异步到达的 pending 在此接续轮询(loading/err 期不抢)
  if (loading.value || err.value) return
  if (s?.status === 'pending') startPoll()
})

const label = computed(() => {
  if (status.value === 'done') return '查看'
  if (status.value === 'err') return '!'
  return '分析'
})
const title = computed(() => {
  if (status.value === 'loading') return 'AI 分析中…'
  if (status.value === 'err') return '分析失败:' + err.value
  if (status.value === 'done') {
    const at = aiState.value?.analyzed_at
    return at ? `查看 AI 报告（${at}）` : '查看 AI 报告'
  }
  return 'AI 分析'
})
</script>

<template>
  <button
    class="ai-btn"
    :class="'st-' + status"
    :disabled="status === 'loading'"
    :title="title"
    @click="onClick"
  >
    <span v-if="status === 'loading'" class="spin"></span>
    <template v-else>
      <i v-if="status === 'done'" class="bar" :style="{ background: sigVar(signal) }"></i>
      <span>{{ label }}</span>
    </template>
  </button>
</template>

<style scoped>
.ai-btn {
  width: 64px;
  height: 28px;
  padding: 0 10px;
  margin-right: 4px;
  font-size: 12px;
  font-weight: 600;
  color: var(--ink-mute);
  background: var(--surface-2);
  border: 1px solid var(--line);
  border-radius: 6px;
  cursor: pointer;
  transition: 0.15s;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  vertical-align: middle;
}
.ai-btn:hover:not(:disabled) {
  border-color: var(--gold-lo);
  color: var(--gold);
}
.ai-btn.st-loading {
  color: var(--ink-faint);
  cursor: progress;
}
.ai-btn.st-done {
  color: var(--ink);
  background: var(--surface);
}
.ai-btn.st-err {
  color: #fff;
  background: var(--up);
  border-color: var(--up);
}
.ai-btn .bar {
  width: 4px;
  height: 14px;
  border-radius: 2px;
  flex-shrink: 0;
}
.ai-btn:disabled {
  cursor: progress;
}
</style>
