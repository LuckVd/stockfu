<script setup lang="ts">
import { ref } from 'vue'
import { NModal } from 'naive-ui'
import ProxyTab from './ProxyTab.vue'
import ScheduleTab from './ScheduleTab.vue'
import MailTab from './MailTab.vue'
import LlmTab from './LlmTab.vue'
import SignalTab from './SignalTab.vue'

defineProps<{ show: boolean }>()
const emit = defineEmits<{ 'update:show': [boolean] }>()

type TabKey = 'proxy' | 'schedule' | 'mail' | 'llm' | 'signals'
const tabs: { key: TabKey; label: string }[] = [
  { key: 'proxy', label: '代理' },
  { key: 'schedule', label: '定时' },
  { key: 'mail', label: '邮件' },
  { key: 'llm', label: 'AI模型' },
  { key: 'signals', label: '策略评分' },
]
const curTab = ref<TabKey>('proxy')
</script>

<template>
  <NModal
    :show="show"
    preset="card"
    title="设置"
    :bordered="false"
    style="max-width: 900px"
    :body-style="{ padding: '8px 24px 20px' }"
    @update:show="(v: boolean) => emit('update:show', v)"
  >
    <div class="set-tabs">
      <button
        v-for="t in tabs"
        :key="t.key"
        :class="{ on: curTab === t.key }"
        @click="curTab = t.key"
      >{{ t.label }}</button>
    </div>
    <!-- v-if 懒加载：首次切到才 mount → 各 tab onMounted(load)，避免开弹窗就发 4 请求 -->
    <ProxyTab v-if="curTab === 'proxy'" />
    <ScheduleTab v-if="curTab === 'schedule'" />
    <MailTab v-if="curTab === 'mail'" />
    <LlmTab v-if="curTab === 'llm'" />
    <SignalTab v-if="curTab === 'signals'" />
  </NModal>
</template>
