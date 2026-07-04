import { defineStore } from 'pinia'
import type { GlobalThemeOverrides } from 'naive-ui'

export interface ThemeDef {
  key: string
  label: string
  swatch: string  // 色块背景
  ink: string     // 色块上的点色
  isDark: boolean
  primary: string // = 旧 --gold,作为 NaiveUI primary
}

// 7 套(沿用旧 index.html 的 data-theme 键 + sf-theme localStorage)
export const THEMES: ThemeDef[] = [
  { key: 'blue',     label: '明亮·蓝',   swatch: '#f5f7fa', ink: '#2563eb', isDark: false, primary: '#2563eb' },
  { key: 'amber',    label: '暖白·琥珀', swatch: '#faf6ee', ink: '#b45309', isDark: false, primary: '#b45309' },
  { key: 'gold',     label: '明亮·金',   swatch: '#faf8f3', ink: '#c9962b', isDark: false, primary: '#c9962b' },
  { key: 'darkgold', label: '深色·金',   swatch: '#15110a', ink: '#e0b341', isDark: true,  primary: '#e0b341' },
  { key: 'morandi',  label: '莫兰迪',    swatch: '#efe9e4', ink: '#a68a7a', isDark: false, primary: '#a68a7a' },
  { key: 'purple',   label: '深色·紫',   swatch: '#140f24', ink: '#a78bfa', isDark: true,  primary: '#a78bfa' },
  { key: 'coral',    label: '明亮·珊瑚', swatch: '#fdf5f3', ink: '#e85a4f', isDark: false, primary: '#e85a4f' },
]

const STORAGE_KEY = 'sf-theme'

function defOf(key: string): ThemeDef {
  return THEMES.find(t => t.key === key) || THEMES[0]
}

function loadInitial(): string {
  const t = localStorage.getItem(STORAGE_KEY) || 'blue'
  return THEMES.some(x => x.key === t) ? t : 'blue'
}

export const useThemeStore = defineStore('theme', {
  state: () => ({ key: loadInitial() }),
  getters: {
    current: (state): ThemeDef => defOf(state.key),
    isDark: (state): boolean => defOf(state.key).isDark,
    overrides: (state): GlobalThemeOverrides => {
      const p = defOf(state.key).primary
      return {
        common: {
          primaryColor: p,
          primaryColorHover: p,
          primaryColorPressed: p,
          primaryColorSuppl: p,
          borderRadius: '8px',
          fontFamily: "'Inter',-apple-system,'Segoe UI',Roboto,system-ui,'PingFang SC','Microsoft YaHei',sans-serif",
        },
      }
    },
  },
  actions: {
    setTheme(key: string) {
      this.key = THEMES.some(t => t.key === key) ? key : 'blue'
      localStorage.setItem(STORAGE_KEY, this.key)
      // 同步 <html data-theme>(驱动 style.css 的 CSS 变量)
      document.documentElement.dataset.theme = this.key === 'blue' ? '' : this.key
    },
  },
})
