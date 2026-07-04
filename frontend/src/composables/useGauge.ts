// 半圆仪表盘 + 表情(移植旧 index.html gauge/moodFace),返回 SVG 字符串,组件 v-html 渲染。
// 依赖全局 .gauge/.face/.num-row/.nil 样式(见 style.css)。

const FACES: Record<string, string[]> = {
  fear: ['😌', '🙂', '😐', '😰', '😱'],
  greed: ['😴', '🙂', '😏', '🤤', '🤑'],
  heat: ['🥶', '🌧️', '🌤️', '☀️', '🥵'],
}

export interface Band {
  label: string
  color: string
}

export function band(v: number | null | undefined): Band {
  if (v == null || (typeof v === 'number' && isNaN(v))) return { label: '—', color: 'var(--ink-faint)' }
  let label: string, color: string
  if (v >= 75) { label = '极强'; color = '#dc2626' }
  else if (v >= 55) { label = '强'; color = 'var(--heat)' }
  else if (v >= 45) { label = '中'; color = 'var(--neutral)' }
  else if (v >= 25) { label = '弱'; color = '#84cc16' }
  else { label = '极弱'; color = '#16a34a' }
  return { label, color }
}

// 半圆仪表盘 + 中心数字 + 表情。key 给定则按档位出表情;heat + chg 用涨跌色(红买热/绿卖热)。
export function gaugeSvg(
  v: number | null | undefined,
  key?: 'fear' | 'greed' | 'heat',
  chg?: number | null,
): string {
  if (v == null || (typeof v === 'number' && isNaN(v))) {
    return '<span class="nil">—</span>'
  }
  let color = band(v).color
  if (key === 'heat' && chg != null) color = chg >= 0 ? '#dc2626' : '#16a34a'
  const len = Math.PI * 42
  const dash = (len * Math.max(0, Math.min(100, v))) / 100
  let face = ''
  if (key && FACES[key]) {
    const i = v >= 75 ? 4 : v >= 55 ? 3 : v >= 45 ? 2 : v >= 25 ? 1 : 0
    face = `<span class="face">${FACES[key][i]}</span>`
  }
  return (
    `<span class="gauge"><svg viewBox="0 0 100 52" width="108" height="56">` +
    `<path d="M 8 50 A 42 42 0 0 1 92 50" fill="none" stroke="var(--line-soft)" stroke-width="9" stroke-linecap="round"/>` +
    `<path d="M 8 50 A 42 42 0 0 1 92 50" fill="none" stroke="${color}" stroke-width="9" stroke-linecap="round" stroke-dasharray="${dash.toFixed(1)} ${len.toFixed(1)}"/></svg>` +
    `<span class="num-row"><b style="color:${color}">${Math.round(v)}</b>${face}</span></span>`
  )
}

export function moodFace(key: 'fear' | 'greed' | 'heat', v: number | null | undefined): string {
  if (v == null || (typeof v === 'number' && isNaN(v))) return ''
  const i = v >= 75 ? 4 : v >= 55 ? 3 : v >= 45 ? 2 : v >= 25 ? 1 : 0
  return FACES[key]?.[i] || ''
}

// 指数胶囊条：填充 value% + 数字。chg=方向(涨跌)：热度用方向色(红买热/绿卖热)；不传=档位色。
// 命中全局 .idx-bar/.track 样式（v-html 注入，scoped 够不到）。
export function indexBar(
  v: number | null | undefined,
  mini?: boolean,
  chg?: number | null,
): string {
  if (v == null || (typeof v === 'number' && isNaN(v))) {
    return '<span class="num mute">—</span>'
  }
  let color = band(v).color
  if (chg != null) color = chg >= 0 ? '#dc2626' : '#16a34a'
  const pct = Math.max(0, Math.min(100, v))
  return (
    `<span class="idx-bar${mini ? ' mini' : ''}"><span class="track">` +
    `<i style="width:${pct}%;background:${color}"></i></span>` +
    `<b style="color:${color}">${Math.round(v)}</b></span>`
  )
}

// 热度信号：N 个 caret 堆叠成 SVG，固定 viewBox 高度恒定；涨=红^ 从底向上，跌=绿∨ 从顶向下。
// 命中全局 .heat-svg.up/.dn path 样式。
export function heatArrows(heat: number | null | undefined, chg?: number | null): string {
  if (heat == null || (typeof heat === 'number' && isNaN(heat))) {
    return '<span class="num mute">—</span>'
  }
  const n = Math.max(1, Math.min(5, Math.ceil(heat / 20)))
  const up = (chg == null ? 0 : chg) >= 0
  const W = 14
  const H = 24
  const cw = 10
  const chh = 6
  const step = 3.6
  const cx = W / 2
  let paths = ''
  for (let i = 0; i < n; i++) {
    let edgeY: number
    let tipY: number
    if (up) {
      edgeY = H - i * step
      tipY = edgeY - chh
    } else {
      edgeY = i * step
      tipY = edgeY + chh
    }
    paths +=
      `<path d="M ${cx - cw / 2} ${edgeY} L ${cx} ${tipY} L ${cx + cw / 2} ${edgeY}" ` +
      `fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>`
  }
  return `<svg class="heat-svg ${up ? 'up' : 'dn'}" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}">${paths}</svg>`
}
