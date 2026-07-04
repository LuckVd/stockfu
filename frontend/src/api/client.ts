// API 客户端:dev 跨域指向 :8787,build 后同源。
// 沿用旧 index.html 的 api():text→JSON 容错 + {error} 归一。
export const BASE_URL = import.meta.env.DEV ? 'http://127.0.0.1:8787' : ''

/** 请求返回值:成功是数据对象/数组,失败是 {error:string}。用 isError() 区分。 */
export type ApiErr = { error: string }

async function request<T = any>(path: string, opt?: RequestInit): Promise<T | ApiErr> {
  try {
    const r = await fetch(BASE_URL + path, opt)
    const txt = await r.text()
    let j: any = null
    try { j = txt ? JSON.parse(txt) : null } catch { /* 非 JSON(如 500 纯文本) */ }
    if (!r.ok) {
      return { error: (j && (j.detail || j.error)) || (txt && txt.slice(0, 200)) || ('HTTP ' + r.status) }
    }
    return j as T
  } catch (e: any) {
    return { error: String(e) }
  }
}

export const doGet = <T = any>(p: string) => request<T>(p)
export const doPost = <T = any>(p: string, body?: any, json = true) =>
  request<T>(p, body !== undefined ? {
    method: 'POST',
    headers: json ? { 'Content-Type': 'application/json' } : undefined,
    body: json ? JSON.stringify(body) : body,
  } : { method: 'POST' })
export const doPut = <T = any>(p: string, body?: any) =>
  request<T>(p, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) })
export const doDel = <T = any>(p: string) => request<T>(p, { method: 'DELETE' })

export function isError<T>(r: T | ApiErr): r is ApiErr {
  return !!r && typeof r === 'object' && 'error' in (r as any) && !!(r as any).error
}
