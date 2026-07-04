// 30 日收盘价 → 渐变面积迷你图几何。返回 {areaPath,linePath,gradId,color} 供模板渲染 <svg>(避开 v-html)。
// viewBox 归一化 100×40,留 4px 上下边距;渐变 id 必须含 advisor 唯一(4 卡同屏不撞)。

export interface SparkGeom {
  areaPath: string
  linePath: string
  gradId: string
  color: string
}

export function miniSpark(closes: number[], color: string, gradId: string): SparkGeom | null {
  const n = closes?.length || 0
  if (n < 2) return null
  const W = 100
  const H = 40
  const pad = 4
  let mn = Infinity
  let mx = -Infinity
  for (const c of closes) {
    if (c < mn) mn = c
    if (c > mx) mx = c
  }
  const range = mx - mn || 1
  const pts: [number, number][] = closes.map((c, i) => {
    const x = (i / (n - 1)) * W
    const y = pad + (H - 2 * pad) * (1 - (c - mn) / range)
    return [x, y]
  })
  const line = pts
    .map((p, i) => `${i === 0 ? 'M' : 'L'}${p[0].toFixed(2)},${p[1].toFixed(2)}`)
    .join(' ')
  const area = `${line} L${W.toFixed(2)},${H} L0,${H} Z`
  return { areaPath: area, linePath: line, gradId, color }
}
