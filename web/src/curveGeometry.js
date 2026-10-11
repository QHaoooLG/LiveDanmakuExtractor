/**
 * 区间曲线的**纯几何计算**（Phase 3）—— 不碰 DOM、不碰 Vue，可直接单测。
 *
 * ## 这一层要解决的事（也是 Phase 3 的全部"技术风险"所在）
 *
 * Phase 3 换的是**交互与样式**，不是数据。所以只要把"像素 ↔ 时间"的换算全部
 * 收敛到这一个模块里，组件本身就没有算错的机会了 —— 这与 Phase 1 把
 * "我在看哪一段"收敛成一个 reducer 是同一个思路。
 *
 * 四件事：
 *
 * 1. **时间 → 曲线坐标**（`xOfTs` / `tsOfX`）：一对互逆映射，DOM 与计算共用；
 * 2. **定义域**（`safeDomain` / `curveDomain`）：轴画在哪一段上；
 * 3. **曲线路径**（`densifyBuckets` / `curvePath`）：空档补零 → 折线 → 面积闭合；
 * 4. **命中与手势**（`hitTest` / `dragAnchor` / `dragEdge` / `dragWindow`）：
 *    每个手势都返回**新的时间戳**，由调用方丢给 reducer —— 组件不自己改状态。
 *
 * ## 坐标约定（写死在这里，免得组件各处理解不同）
 *
 * 用 **viewBox 坐标**（不是屏幕像素）：`x ∈ [0, width]` 从左到右、`y ∈ [0, height]`
 * 从上到下（`y = height` 是基线）。屏幕像素换 viewBox 只在一处做
 * （`toViewBox`），而 SVG 自己会把 viewBox 缩放到实际尺寸。
 *
 * ## 2026-10-09（第三/四轮）改掉的三件事（都是真机实测驱动的）
 *
 * 1. **轴绝不超出数据范围**（`curveDomain`）：早期版本"把窗口并进定义域"，
 *    于是点最左端时轴被窗口撑大、曲线上凭空多出一段基线；
 *    现在窗口超出数据时**只把轴贴到数据边缘**，多出来的部分由调用方夹回去；
 * 2. **空档要补零**（`densifyBuckets`）：后端只返回"有数据的桶"，
 *    直接把稀疏桶连起来会把"没人说话的一小时"画成一条斜坡；
 * 3. **单个桶画平台**（`curvePath`）：一个点画不出线（`M` 之后没有任何 `L`），
 *    而 `bucket_s` 是已知的，所以按"这一格多宽"画一段水平线。
 */

/** 曲线顶端留白（px）：柱子顶到天花板会看不出发掘空间 */
export const TOP_PAD = 6
/** 最小拖动识别距离（px，viewBox 单位）：小于它当"点击"处理 */
export const CLICK_SLOP = 3
/**
 * 补零后最多允许多少个槽位。
 *
 * `densifyBuckets` 会按 `range / bucketS` 铺满整个数据范围 —— 窗口跨度大、
 * 格宽又小的时候（例如"看 12 小时，每格 1 分钟"= 720 格）这仍然可控，
 * 但真库上出现过"跨好几天 + 1 分钟格"这种量级（十万级槽位）。
 * 超过上限就**放弃补零**（宁可画得糙一点，也不能让浏览器卡死）。
 */
export const MAX_DENSE_SLOTS = 5000

/**
 * 屏幕坐标 → viewBox 坐标。
 *
 * `rect` 是 SVG 元素的 `getBoundingClientRect()`。缩放比取两个方向里较小的那个
 * （`preserveAspectRatio` 的默认行为是 `xMidYMid meet`）—— 但本模块的调用方
 * 会把 viewBox 设成与 CSS 尺寸同比例，所以正常情况下 x/y 缩放比相等。
 * 这里仍然各算各的，是为了在尺寸短暂不一致时**不引入横向偏移**。
 */
export function toViewBox(clientX, clientY, rect, viewBox) {
  const width = Number(rect?.width) || 1
  const height = Number(rect?.height) || 1
  return {
    x: ((Number(clientX) - (rect?.left ?? 0)) / width) * viewBox.width,
    y: ((Number(clientY) - (rect?.top ?? 0)) / height) * viewBox.height,
  }
}

/**
 * `null` / `''` / `NaN` / `Infinity` 一律当"没有值"。
 *
 * ⚠️ **不要用 `Number(x)` 判有值**：`Number(null) === 0`、`Number('') === 0`，
 * 于是"没有窗口"会被当成"窗口在时间戳 0"（1970 年）—— 轴被拉到 1970 年，
 * 曲线挤成右侧一根竖线。这个坑在真机上表现为"曲线完全不对"。
 */
export function toFiniteOrNull(value) {
  if (value == null || value === '') return null
  const numeric = Number(value)
  return Number.isFinite(numeric) ? numeric : null
}

/** 曲线若为空时的兜底定义域（避免除零），`valid: false` 表示"这不是真数据范围" */
export function safeDomain(range) {
  const lo = toFiniteOrNull(range?.lo)
  const hi = toFiniteOrNull(range?.hi)
  if (lo == null || hi == null || hi <= lo) {
    const fallback = lo ?? 0
    return { lo: fallback, hi: fallback + 1, valid: false }
  }
  return { lo, hi, valid: true }
}

/**
 * 画出曲线的**定义域**：**以数据范围为准**，窗口只把轴扩到数据边缘。
 *
 * 三条规则（顺序不能换）：
 *
 * 1. **对齐到格边界**：`bucket_s` 是 epoch 对齐的（后端按 `int(recv_ts/step)` 分桶），
 *    所以轴也按格对齐 —— 否则第一个/最后一个桶会画出半格，看起来像"曲线被切了"；
 * 2. **窗口只在数据范围内扩轴**：`lo = min(lo, max(wLo, data.lo))`。
 *    早期版本直接 `min(lo, wLo)`，于是窗口探出数据 30 秒时轴也多出 30 秒 ——
 *    曲线左端凭空出现一段没有数据的基线；
 * 3. 数据完全无效时（`valid: false`）才退回用窗口当轴。
 */
export function curveDomain(range, window_, bucketS = 0) {
  const data = safeDomain(range)
  const wLo = Number(window_?.lo)
  const wHi = Number(window_?.hi)
  const hasWindow = Number.isFinite(wLo) && Number.isFinite(wHi) && wHi > wLo

  if (!data.valid) return hasWindow ? { lo: wLo, hi: wHi } : { lo: data.lo, hi: data.hi }

  let lo = data.lo
  let hi = data.hi
  const step = Number(bucketS)
  if (step > 0) {
    lo = Math.floor(lo / step) * step
    hi = Math.floor(hi / step) * step + step
  }
  if (hasWindow) {
    lo = Math.min(lo, Math.max(wLo, data.lo))
    hi = Math.max(hi, Math.min(wHi, data.hi))
  }
  if (hi <= lo) hi = lo + 1
  return { lo, hi }
}

/** 某个时刻在曲线上的 x（viewBox 坐标）。 */
export function xOfTs(ts, { domain, width }) {
  const span = domain.hi - domain.lo
  if (!(span > 0)) return 0
  return ((Number(ts) - domain.lo) / span) * width
}

/** 某个 x（viewBox 坐标）对应的时间戳 —— `xOfTs` 的逆映射。 */
export function tsOfX(x, { domain, width }) {
  const span = domain.hi - domain.lo
  if (!(width > 0)) return domain.lo
  return domain.lo + (Number(x) / width) * span
}

/**
 * 把稀疏的密度桶**补成等间隔的一整排**（空档补 0）。
 *
 * 为什么必须有这一步：`/api/density` 只返回**有数据的桶**。直接把这些桶连成折线，
 * "中间那两小时没人说话"会被画成一条从左边高点斜到右边高点的**斜坡** ——
 * 用户看到的是"这段时间一直有人说话，只是少了点"，与事实相反（同类症状①）。
 *
 * 补零的范围是**数据范围**（`range.lo..range.hi`）按 `bucket_s` 对齐后的所有格，
 * 与轴的定义域（`curveDomain`）口径一致。
 *
 * 三种情况**原样返回**（不补零），因为补了反而更糟：
 *
 * * 没有 `bucketS` 或没有 `range`（不知道格宽/范围）；
 * * 范围非法（`hi < lo`）；
 * * 槽位数超过 `maxSlots`（大跨度 + 细粒度，补零会成为新的性能问题）。
 *
 * ⚠️ 注意最后一格：`hi` 落在哪一格是 `floor(hi / step)`，与轴的 `floor(hi/l)*l + l`
 * （轴要多画半格到整格边界）**故意不同** —— 轴负责"看起来完整"，补零负责"数据多少"。
 */
export function densifyBuckets(buckets, { bucketS, range, maxSlots = MAX_DENSE_SLOTS } = {}) {
  const list = (buckets ?? []).filter((bucket) => Number.isFinite(Number(bucket?.ts)))
  const step = Number(bucketS)
  const normalized = list.map((bucket) => ({
    ts: Number(bucket.ts),
    count: Number(bucket.count) || 0,
    by_type: bucket.by_type ?? null,
  }))

  if (!normalized.length || !(step > 0) || !range) return normalized
  const lo = Number(range.lo)
  const hi = Number(range.hi)
  if (!Number.isFinite(lo) || !Number.isFinite(hi) || hi < lo) return normalized

  const first = Math.floor(lo / step)
  const last = Math.floor(hi / step)
  const slots = last - first + 1
  if (!Number.isFinite(slots) || slots <= 0 || slots > maxSlots) return normalized

  const counts = new Map()
  for (const bucket of normalized) {
    const key = Math.floor(bucket.ts / step)
    counts.set(key, (counts.get(key) ?? 0) + bucket.count)
  }
  const out = []
  for (let slot = first; slot <= last; slot += 1) {
    out.push({ ts: slot * step, count: counts.get(slot) ?? 0, by_type: null })
  }
  return out
}

/**
 * 曲线路径。
 *
 * 返回 `{ line, area }` 两段 `d`：
 *   * `line`：折线，x 取**桶中心**（`ts + bucketS / 2`）——
 *     桶代表的是"这一格"，画在格子起点会让整条曲线系统性左偏半格；
 *   * `area`：同一条折线闭合到底边，用来填色。
 *
 * `y` 由 **P95 归一化**（沿用 `timeline.peakOf`）—— 用最大值的话，一次涌入
 * 几百条会把其余全压成一条平线。
 *
 * **只有一个桶时画平台**：那样本来只有一个点，`M` 之后没有任何 `L`，
 * 浏览器什么都不画（"曲线不显示"的经典原因）。这里按 `bucketS` 的宽度
 * 画一段水平线，让它至少是一段可见的"台阶"。
 *
 * 空数据返回空串（调用方据此画"暂无数据"提示）。
 */
export function curvePath(buckets, { domain, width, height, peak, bucketS }) {
  const list = buckets ?? []
  if (!list.length || !(width > 0) || !(height > 0)) return { line: '', area: '' }
  const usable = Math.max(height - TOP_PAD, 1)
  const max = Number(peak) > 0 ? Number(peak) : 1
  const step = Number(bucketS) > 0 ? Number(bucketS) : 0
  const yOf = (count) => height - Math.min(Math.max(Number(count) / max, 0), 1) * usable

  if (list.length === 1) {
    const ts = Number(list[0].ts)
    const x1 = step > 0 ? xOfTs(ts, { domain, width }) : 0
    const x2 = step > 0 ? xOfTs(ts + step, { domain, width }) : width
    const left = Math.max(0, Math.min(x1, x2))
    const right = Math.min(width, Math.max(x1, x2, left + 1))
    const y = round(yOf(list[0].count))
    const line = `M${round(left)},${y} L${round(right)},${y}`
    return { line, area: `${line} L${round(right)},${height} L${round(left)},${height} Z` }
  }

  const points = list.map((bucket) => {
    const ts = Number(bucket.ts)
    return { x: xOfTs(step > 0 ? ts + step / 2 : ts, { domain, width }), y: yOf(bucket.count) }
  })
  const line = points
    .map((point, index) => `${index === 0 ? 'M' : 'L'}${round(point.x)},${round(point.y)}`)
    .join(' ')
  const first = points[0]
  const last = points[points.length - 1]
  const area = `${line} L${round(last.x)},${height} L${round(first.x)},${height} Z`
  return { line, area }
}

/** 保留两位小数就够了（路径会长很多，多余精度只浪费字节） */
function round(value) {
  return Math.round(Number(value) * 100) / 100
}

/**
 * 时间刻度：给 4–6 个"整齐"的时刻。
 *
 * 为什么只给 4–6 个：整库按 1 分钟切会有几万格，每格画一个刻度会糊成一片。
 * 步长从"人类可读"的档位里挑（1/2/5/10/15/30 分钟、1/2/6/12/24 小时），
 * 挑到让刻度数落进 `[4, 8]` 为止。
 */
export function timeTicks(domain, { width, maxTicks = 6 } = {}) {
  const span = domain.hi - domain.lo
  if (!(span > 0) || !(width > 0)) return []
  const candidates = [
    60, 120, 300, 600, 900, 1800,
    3600, 7200, 21600, 43200, 86400,
  ]
  const ideal = span / Math.max(maxTicks, 1)
  const step = candidates.find((value) => value >= ideal) ?? candidates[candidates.length - 1]
  const ticks = []
  // 从"第一个 >= lo 的整齐时刻"开始
  const first = Math.ceil(domain.lo / step) * step
  for (let ts = first; ts <= domain.hi && ticks.length <= maxTicks + 1; ts += step) {
    ticks.push({ ts, x: xOfTs(ts, { domain, width }) })
  }
  return ticks
}

/**
 * 命中测试：点在曲线的哪个部位。
 *
 * 返回 `{ kind, ... }`：
 *   * `{ kind: 'handle', edge: 'lo' | 'hi' }` —— 点在窗口带的某一端把手上；
 *   * `{ kind: 'window' }` —— 点在窗口带中间（可整体平移）；
 *   * `{ kind: 'curve' }` —— 点在曲线上（移动播放头）。
 *
 * 把手优先于窗口带：把手在带的边缘，两者重叠时必须先判把手，否则永远拖不到宽度。
 */
export function hitTest(x, window_, { domain, width, handlePx = 8 }) {
  const wLo = Number(window_?.lo)
  const wHi = Number(window_?.hi)
  const hasWindow = Number.isFinite(wLo) && Number.isFinite(wHi) && wHi > wLo
  if (!hasWindow) return { kind: 'curve' }
  const xLo = xOfTs(wLo, { domain, width })
  const xHi = xOfTs(wHi, { domain, width })
  if (Math.abs(x - xLo) <= handlePx) return { kind: 'handle', edge: 'lo' }
  if (Math.abs(x - xHi) <= handlePx) return { kind: 'handle', edge: 'hi' }
  if (x > xLo && x < xHi) return { kind: 'window' }
  return { kind: 'curve' }
}

/**
 * 拖动播放头 / 在曲线上点击 → 目标时间戳。
 *
 * 落在定义域外时**夹到边界**（用户拖出图外不应该产生一个越界时刻）。
 */
export function dragAnchor(x, { domain, width }) {
  const ts = tsOfX(x, { domain, width })
  return Math.min(Math.max(ts, domain.lo), domain.hi)
}

/**
 * 拖窗口带的某一端：只改这一端，**另一端固定**。
 *
 * 返回新的 `{ lo, hi }`（还没过段长上下限 —— 那是 reducer 的事，
 * 这里只负责"几何上该落在哪"）。
 *
 * ⚠️ 两端会**交叉**：用户把左端拖到右端右边时，返回的 `lo > hi`。
 * 调用方应当交换它们，而不是把窗口拖成负宽度（历史上有过"给反了就自动正过来"
 * 的先例，见标注区间导出）。
 */
export function dragEdge(edge, x, window_, { domain, width, minSpanS = 1 }) {
  const ts = dragAnchor(x, { domain, width })
  const wLo = Number(window_?.lo)
  const wHi = Number(window_?.hi)
  if (!Number.isFinite(wLo) || !Number.isFinite(wHi)) return { lo: ts, hi: ts + minSpanS }
  if (edge === 'lo') {
    const lo = Math.min(ts, wHi - minSpanS)
    return { lo: Math.max(lo, domain.lo), hi: wHi }
  }
  const hi = Math.max(ts, wLo + minSpanS)
  return { lo: wLo, hi: Math.min(hi, domain.hi) }
}

/**
 * 拖窗口带中间：整体平移，**段长不变**。
 *
 * 平移到数据边缘时**贴边停住**（不压缩窗口）—— 压缩会让"我选了 15 分钟"
 * 在拖到边缘时变成 3 分钟，用户会以为窗口坏了。
 */
export function dragWindow(deltaTs, window_, { domain }) {
  const wLo = Number(window_?.lo)
  const wHi = Number(window_?.hi)
  if (!Number.isFinite(wLo) || !Number.isFinite(wHi) || !(wHi > wLo)) {
    return { lo: domain.lo, hi: domain.hi }
  }
  const span = wHi - wLo
  let lo = wLo + Number(deltaTs || 0)
  lo = Math.min(Math.max(lo, domain.lo), domain.hi - span)
  if (!Number.isFinite(lo)) lo = domain.lo
  return { lo, hi: lo + span }
}

/**
 * 滚轮 → 新的段长（以**光标处**为中心缩放）。
 *
 * 返回 `{ anchorTs, spanS }`：调用方把它丢给 reducer 的 `point`（自带 spanS）。
 * 以光标为中心（而不是以窗口中心）是"放大我看的那一块"的直觉行为。
 *
 * `factor > 1` 放大（段长变小），`factor < 1` 缩小。
 */
export function wheelZoom(x, factor, window_, { domain, width, minSpanS, maxSpanS }) {
  const wLo = Number(window_?.lo)
  const wHi = Number(window_?.hi)
  const span = Number.isFinite(wLo) && Number.isFinite(wHi) && wHi > wLo
    ? wHi - wLo
    : (Number(maxSpanS) || 900)
  const min = Number(minSpanS) || 60
  const max = Number(maxSpanS) || 900
  const next = Math.min(Math.max(span / (Number(factor) || 1), min), max)
  const pivot = dragAnchor(x, { domain, width })
  // 保持"光标下的时刻"仍在同一个相对位置：新窗口 = 以 pivot 为中心
  const anchorTs = Number.isFinite(pivot) ? pivot : (domain.lo + domain.hi) / 2
  return { anchorTs, spanS: next }
}
