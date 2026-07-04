import { createApp } from 'vue'
import { createPinia } from 'pinia'
import naive from 'naive-ui'
import './style.css'
import App from './App.vue'

// 早早应用主题(避免刷新闪烁);key 与 stores/theme 的 localStorage('sf-theme')一致。
const _t = localStorage.getItem('sf-theme') || 'blue'
document.documentElement.dataset.theme = _t === 'blue' ? '' : _t

createApp(App).use(createPinia()).use(naive).mount('#app')
