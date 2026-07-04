import { onUnmounted } from 'vue'
import { aiApi } from '@/api/modules'
import { isError } from '@/api/client'
import type { AiResultResp, AiSignal } from '@/api/types'

// 轮询 AI 分析结果:pending 时每 5s 查 /ai/result,done/none 即停,3 分钟超时。
// 实例级 timer,组件卸载自动清理。
export function useAiPoll() {
  let timer = 0

  function clear() {
    if (timer) {
      clearInterval(timer)
      timer = 0
    }
  }

  function pollAiResult(
    code: string,
    onDone: (r: AiResultResp) => void,
    onSignal?: (sig: AiSignal | null | undefined) => void,
  ) {
    clear()
    const t0 = Date.now()
    timer = window.setInterval(async () => {
      if (Date.now() - t0 > 3 * 60 * 1000) {
        clear()
        return
      }
      const r = await aiApi.result(code)
      if (isError(r)) return
      if (r.status === 'done' || r.status === 'none') {
        clear()
        onDone(r)
      } else if (onSignal && r.signal) {
        onSignal(r.signal)
      }
    }, 5000)
  }

  onUnmounted(clear)
  return { pollAiResult, clear }
}
