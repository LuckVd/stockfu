import { onUnmounted } from 'vue'
import { indicesApi } from '@/api/modules'
import { isError } from '@/api/client'

// 轮询个股指数是否就绪：ensureStock 触发后台补 K 线 + 算情绪，fear 落库即视为就绪。
// 实例级 timer（后调用覆盖前调用），组件卸载自动清理，避免泄漏。
export function usePoll() {
  let timer = 0

  function clear() {
    if (timer) {
      clearTimeout(timer)
      timer = 0
    }
  }

  function pollStockReady(code: string, onReady: () => void, times = 6) {
    clear()
    if (times <= 0) return
    timer = window.setTimeout(async () => {
      timer = 0
      const r = await indicesApi.stock(code)
      if (!isError(r) && r.fear != null) {
        onReady()
      } else if (times > 1) {
        pollStockReady(code, onReady, times - 1)
      }
    }, 8000)
  }

  onUnmounted(clear)
  return { pollStockReady, clear }
}
