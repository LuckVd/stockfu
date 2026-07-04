// 通用排序（移植旧 index.html sortBy 行 890-894 + sortHoldings/sortWatchlist 默认逻辑）。
// null/''/NaN 统一排末尾（与升降序无关）；字符串走 localeCompare('zh')。

export type SortDir = 'asc' | 'desc'
export interface SortState {
  key: string | null
  dir: SortDir
}

// 默认升序的列（字符串列）；其余列默认降序。
const STRING_COLS = new Set(['code', 'name', 'currency'])

function hasVal(v: any): boolean {
  return v != null && v !== '' && !(typeof v === 'number' && isNaN(v))
}

export function sortBy<T extends Record<string, any>>(arr: T[], key: string | null, dir: SortDir): T[] {
  if (!key) return arr
  const yes = arr.filter((x) => hasVal(x[key]))
  const no = arr.filter((x) => !hasVal(x[key]))
  yes.sort((a, b) => {
    const va = a[key]
    const vb = b[key]
    const r = typeof va === 'string' ? String(va).localeCompare(String(vb), 'zh') : va - vb
    return dir === 'asc' ? r : -r
  })
  return [...yes, ...no]
}

// 同列切 asc/desc；新列按列类型默认方向。
export function nextSort(cur: SortState, key: string): SortState {
  if (cur.key === key) return { key, dir: cur.dir === 'asc' ? 'desc' : 'asc' }
  return { key, dir: STRING_COLS.has(key) ? 'asc' : 'desc' }
}
