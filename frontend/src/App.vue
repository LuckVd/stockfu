<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { NConfigProvider, darkTheme, NMessageProvider, NDialogProvider } from 'naive-ui'
import { useThemeStore } from '@/stores/theme'
import { usePortfolioStore } from '@/stores/portfolio'
import AppHeader from '@/components/layout/AppHeader.vue'
import MarketMood from '@/components/dashboard/MarketMood.vue'
import Summary from '@/components/dashboard/Summary.vue'
import MainTabs from '@/components/dashboard/MainTabs.vue'

const theme = useThemeStore()
const nTheme = computed(() => (theme.isDark ? darkTheme : null))
const portfolio = usePortfolioStore()

// 初始拉组合（Summary + HoldingsTable 共享）。watchlist/sentiment 由 MainTabs 懒加载。
onMounted(() => portfolio.fetch())
</script>

<template>
  <NConfigProvider :theme="nTheme" :theme-overrides="theme.overrides">
    <NMessageProvider>
      <NDialogProvider>
        <div class="wrap">
          <AppHeader />
          <MarketMood />
          <Summary />
          <MainTabs />
        </div>
      </NDialogProvider>
    </NMessageProvider>
  </NConfigProvider>
</template>
