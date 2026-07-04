<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { indicesApi } from '@/api/modules'
import { isError } from '@/api/client'
import { gaugeSvg, moodFace } from '@/composables/useGauge'

type Quote = { name?: string; price?: number | null; pct_chg?: number | null; fear?: number | null; greed?: number | null; heat?: number | null }
type QuotesResp = { trade_date?: string } & Record<string, Quote>

const q = ref<QuotesResp | null>(null)
const err = ref('')
const loading = ref(true)

onMounted(async () => {
  const r = await indicesApi.quotes()
  loading.value = false
  if (isError(r)) { err.value = r.error; return }
  q.value = r as QuotesResp
})

const tradeDate = computed(() => {
  const d = q.value?.trade_date
  if (!d) return ''
  const [yy, mm, dd] = d.split('-').map(Number)
  const td = new Date(yy, mm - 1, dd)
  return `${yy}-${String(mm).padStart(2, '0')}-${String(dd).padStart(2, '0')} 周${'日一二三四五六'[td.getDay()]}`
})

const sh = computed(() => q.value?.['000001'])
const shColor = computed(() => dir(sh.value?.pct_chg))
const subs = computed(() => ['399006', '000688'].map(c => q.value?.[c]).filter((x): x is Quote => !!x))

function dir(c?: number | null): string { return c == null ? '#64748b' : c >= 0 ? '#dc2626' : '#16a34a' }
function fmtPct(c?: number | null) { return c == null ? '—' : (c >= 0 ? '+' : '') + Number(c).toFixed(2) + '%' }
function fmtNum(n?: number | null, d = 2) {
  return n == null || isNaN(n as number) ? '—' : Number(n).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d })
}
function rnd(v?: number | null) { return v == null ? '—' : Math.round(v) }
</script>

<template>
  <div v-if="loading" class="loading"><span class="spin"></span> 加载指数中…</div>
  <div v-else-if="err" class="loading err">指数加载失败:{{ err }}</div>
  <template v-else>
    <div v-if="tradeDate" class="hdr-date">{{ tradeDate }}</div>
    <section v-if="sh" class="idx-main">
      <div class="im-title">{{ sh.name }}</div>
      <div class="im-grid">
        <div class="im-num">
          <span class="im-price" :style="{ color: shColor }">{{ fmtNum(sh.price) }}</span>
          <span class="im-chg" :style="{ color: shColor }">{{ fmtPct(sh.pct_chg) }}</span>
        </div>
        <div class="im-cell"><span class="k">恐慌</span><div v-html="gaugeSvg(sh.fear, 'fear')"></div></div>
        <div class="im-cell"><span class="k">贪婪</span><div v-html="gaugeSvg(sh.greed, 'greed')"></div></div>
        <div class="im-cell"><span class="k">热度</span><div v-html="gaugeSvg(sh.heat, 'heat', sh.pct_chg)"></div></div>
      </div>
    </section>
    <div class="mood-sub">
      <div v-for="x in subs" :key="x.name" class="sub-card">
        <div class="sn">{{ x.name }}</div>
        <div class="spr" :style="{ color: dir(x.pct_chg) }">
          {{ fmtNum(x.price) }}<b>{{ fmtPct(x.pct_chg) }}</b>
        </div>
        <div class="ix">
          <span>恐慌 {{ moodFace('fear', x.fear) }} <b>{{ rnd(x.fear) }}</b></span>
          <span>贪婪 {{ moodFace('greed', x.greed) }} <b>{{ rnd(x.greed) }}</b></span>
          <span>热度 {{ moodFace('heat', x.heat) }} <b :style="{ color: dir(x.pct_chg) }">{{ rnd(x.heat) }}</b></span>
        </div>
      </div>
    </div>
  </template>
</template>

<style scoped>
.loading { padding: 40px 0; text-align: center; color: var(--ink-mute); font-size: 14px; }
.loading.err { color: var(--up); }
.hdr-date { font-size: 13px; color: var(--ink-mute); margin-bottom: 10px; }
.idx-main { background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 14px 20px; box-shadow: var(--shadow); }
.im-title { font-size: 15px; font-weight: 700; color: var(--ink); margin-bottom: 10px; }
.im-grid { display: grid; grid-template-columns: 1.3fr 1fr 1fr 1fr; gap: 8px; align-items: center; }
.im-num { display: flex; flex-direction: column; gap: 2px; }
.im-price { font-size: 30px; font-weight: 700; font-variant-numeric: tabular-nums; line-height: 1.1; }
.im-chg { font-size: 16px; font-weight: 600; font-variant-numeric: tabular-nums; }
.im-cell { display: flex; flex-direction: column; align-items: center; gap: 2px; }
.im-cell .k { font-size: 15px; color: var(--ink-mute); font-weight: 500; }
.mood-sub { display: flex; gap: 12px; margin-top: 12px; }
.sub-card { flex: 1; background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 12px 16px; font-size: 12px; box-shadow: var(--shadow); }
.sub-card .sn { font-weight: 600; color: var(--ink-dim); font-size: 13px; margin-bottom: 4px; }
.sub-card .spr { font-size: 18px; font-weight: 700; font-variant-numeric: tabular-nums; margin-bottom: 8px; }
.sub-card .spr b { font-size: 13px; font-weight: 600; margin-left: 6px; }
.sub-card .ix { display: flex; gap: 10px; color: var(--ink-mute); justify-content: space-between; border-top: 1px solid var(--line-soft); padding-top: 8px; }
.sub-card .ix b { font-variant-numeric: tabular-nums; font-weight: 600; margin-left: 3px; }
@media (max-width: 760px) { .im-grid { grid-template-columns: 1fr 1fr; } .mood-sub { flex-direction: column; } }
</style>
