<script setup lang="ts">
import { NModal } from 'naive-ui'

defineProps<{ show: boolean }>()
const emit = defineEmits<{ 'update:show': [boolean] }>()
</script>

<template>
  <NModal
    :show="show"
    preset="card"
    title="情绪指数怎么算"
    :bordered="false"
    style="max-width: 560px"
    :body-style="{ padding: '8px 24px 20px' }"
    @update:show="(v: boolean) => emit('update:show', v)"
  >
    <div class="desc">三个指数均为 0–100，采用 CNN 式算法：每个因子取近 5 年历史分位，再按方向等权平均。</div>
    <div class="help-item"><b>恐慌指数</b>下行因子（波动率分位 + 跌幅分位）。值越高 = 市场越恐慌。</div>
    <div class="help-item"><b>贪婪指数</b>上行因子（涨幅分位 + 成交分位 + 资金流入 / 连板 / 两融）。值越高 = 越贪婪。</div>
    <div class="help-item"><b>热度指数</b>成交活跃度分位。值越高 = 越放量。</div>
    <div class="help-item"><b>表情档位</b>每个指数从低到高对应 5 档表情：恐慌 😌🙂😐😰😱；贪婪 😌🙂😐😁🤑；热度 ❄️🌤️😐🥵🔥。</div>
    <div class="help-item"><b>热度方向</b>热度按当日涨跌着色：<span class="upc">红 = 买入热</span>（涨）、<span class="dnc">绿 = 卖出热</span>（跌）；数字是强度（0-100）。</div>
    <div class="desc" style="margin-top: 12px">三层粒度：大盘（沪深300 基准）/ 板块 / 个股，算法相同。点击持仓表里的代码，可看个股各因子的分位明细。</div>
  </NModal>
</template>
