// 数字/百分比格式化（移植旧 index.html nf/pct/signed，行 775-777）。null/NaN → '—'。

export function nf(n: number | null | undefined, d = 0): string {
  if (n == null || (typeof n === 'number' && isNaN(n))) return '—'
  return Number(n).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d })
}

export function pct(n: number | null | undefined): string {
  if (n == null || (typeof n === 'number' && isNaN(n))) return '—'
  return (n >= 0 ? '+' : '') + Number(n).toFixed(1) + '%'
}

export function signed(n: number | null | undefined, d = 0): string {
  if (n == null || (typeof n === 'number' && isNaN(n))) return '—'
  return (n >= 0 ? '+' : '') + nf(n, d)
}

// 币种符号
export function curSym(c?: string | null): string {
  if (c === 'USD') return '$'
  if (c === 'HKD') return 'HK$'
  return '¥'
}
