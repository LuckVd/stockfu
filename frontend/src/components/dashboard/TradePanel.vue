<script setup lang="ts">
import { ref, computed } from 'vue'
import { useMessage } from 'naive-ui'
import { usePortfolioStore } from '@/stores/portfolio'
import { useWatchlistStore } from '@/stores/watchlist'
import { tradeApi, stockApi, watchApi } from '@/api/modules'
import { isError } from '@/api/client'
import { usePoll } from '@/composables/usePoll'
import { nf } from '@/composables/useFormat'

type Side = 'buy' | 'sell' | 'track'
interface Recent { side: Side; code: string; shares: string; price: string; t: string }

const emit = defineEmits<{ 'switch-tab': ['watchlist'] }>()
const message = useMessage()
const portfolio = usePortfolioStore()
const watchlist = useWatchlistStore()
const { pollStockReady } = usePoll()

const side = ref<Side>('buy')
const code = ref('')
const shares = ref('')
const price = ref('')
const date = ref('')
const submitting = ref(false)
const msg = ref<{ cls: string; text: string }>({ cls: '', text: '' })
const recents = ref<Recent[]>([])

const submitText = computed(() =>
  side.value === 'buy' ? '确认买入' : side.value === 'sell' ? '确认卖出' : '确认追踪',
)
const submitStyle = computed(() => ({
  color: side.value === 'buy' ? 'var(--up)' : side.value === 'sell' ? 'var(--down)' : 'var(--gold)',
  borderColor:
    side.value === 'buy'
      ? 'var(--up-dim)'
      : side.value === 'sell'
        ? 'var(--down-dim)'
        : 'var(--gold-lo)',
}))

function setSide(s: Side) {
  side.value = s
}
function nowTime() {
  return new Date().toLocaleTimeString('zh-CN', { hour12: false })
}
function addRecent(s: Side, c: string, sh: string, pr: string) {
  recents.value.unshift({ side: s, code: c, shares: sh, price: pr, t: nowTime() })
  if (recents.value.length > 5) recents.value.pop()
}

function ensureStock(c: string) {
  void stockApi.ensure(c) // fire-and-forget（doPost 内部已 try/catch，不会 reject）
  message.success(`${c} 后台补数据中，完成后自动刷新`)
  pollStockReady(c, async () => {
    message.success(`${c} 数据已就绪`)
    await Promise.all([portfolio.fetch(), watchlist.fetch()])
  })
}

async function submit() {
  const c = code.value.trim()
  if (!c) {
    msg.value = { cls: 'err', text: '请填代码' }
    return
  }

  // 追踪分支：只加自选，不产生持仓
  if (side.value === 'track') {
    msg.value = { cls: 'info', text: '加入追踪…' }
    submitting.value = true
    const r = await watchApi.add(c)
    submitting.value = false
    if (isError(r)) {
      msg.value = { cls: 'err', text: '失败：' + r.error }
      return
    }
    msg.value = { cls: 'ok', text: `✓ 已追踪 ${c}（后台补数据中）` }
    message.success(`已追踪：${c}`)
    code.value = ''
    ensureStock(c)
    emit('switch-tab', 'watchlist')
    return
  }

  // 买卖分支
  const sh = shares.value.trim()
  const pr = price.value.trim()
  if (!sh || !pr) {
    msg.value = { cls: 'err', text: '请填齐 代码 / 股数 / 价格' }
    return
  }
  msg.value = { cls: 'info', text: '录入中…' }
  submitting.value = true
  const r = await tradeApi.trade({
    code: c,
    side: side.value,
    shares: Number(sh),
    price: Number(pr),
    date: date.value || undefined,
  })
  submitting.value = false
  if (isError(r)) {
    msg.value = { cls: 'err', text: '失败：' + r.error }
    return
  }
  const verb = side.value === 'buy' ? '买入' : '卖出'
  msg.value = { cls: 'ok', text: `✓ ${verb} ${c} → 持仓 ${nf(r.shares)}股 成本${nf(r.avg_cost, 4)}` }
  message.success(`${verb}成功：${c} ${nf(Number(sh))}股 @ ${pr}`)
  addRecent(side.value, c, sh, pr)
  shares.value = ''
  price.value = ''
  await portfolio.fetch()
  ensureStock(c)
}
</script>

<template>
  <div class="panel">
    <div class="panel-head">
      <h2>交易录入<span class="en">TRADE</span></h2>
      <span class="hint">移动加权平均</span>
    </div>
    <div class="trade" :class="{ 'mode-track': side === 'track' }">
      <div class="seg">
        <button :class="{ on: side === 'buy' }" class="buy" @click="setSide('buy')">买入 BUY</button>
        <button :class="{ on: side === 'sell' }" class="sell" @click="setSide('sell')">卖出 SELL</button>
        <button :class="{ on: side === 'track' }" class="track" @click="setSide('track')">追踪 TRACK</button>
      </div>
      <div class="field">
        <label>代码 CODE</label>
        <input v-model="code" placeholder="600519 / AAPL / HK00700" @keyup.enter="submit">
      </div>
      <div class="field f-trade">
        <label>股数 SHARES</label>
        <input v-model="shares" inputmode="decimal" placeholder="100">
      </div>
      <div class="field f-trade">
        <label>价格 PRICE</label>
        <input v-model="price" inputmode="decimal" placeholder="1500.50">
      </div>
      <div class="field f-trade">
        <label>日期 DATE <small>可选，默认今天</small></label>
        <input v-model="date" placeholder="YYYY-MM-DD">
      </div>
      <button class="btn submit" :disabled="submitting" :style="submitStyle" @click="submit">
        {{ submitText }}
      </button>
      <div class="trade-msg" :class="msg.cls">
        <span v-if="msg.cls === 'info'" class="spin"></span>{{ msg.text }}
      </div>
      <div v-if="recents.length" class="recent">
        <div class="k">最近成交</div>
        <div v-for="(r, i) in recents" :key="i" class="row">
          <span :style="{ color: r.side === 'buy' ? 'var(--up)' : 'var(--down)' }">
            {{ r.side === 'buy' ? '买' : '卖' }} {{ r.code }}
          </span>
          <span>{{ nf(Number(r.shares)) }} @ {{ r.price }}</span>
          <span style="color: var(--ink-faint)">{{ r.t }}</span>
        </div>
      </div>
    </div>
  </div>
</template>
