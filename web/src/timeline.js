/**
 * 时间轴密度条的**共用逻辑**（DOM 版与 canvas 版都从这里取）。
 *
 * 为什么必须抽出来：这一轮把时间轴改成 canvas 绘制，同时**保留 DOM 版做回退**
 * （需求方明确要求"留旧版以便回滚"）。两份实现如果各自算一遍"柱子的高度比例、
 * 焦点格下标、概要行"，两边一定会慢慢不一致 —— 用户切一次渲染方式就会看到
 * 不同的数字。所以凡是**不依赖渲染方式**的东西都放这里，两个组件只负责画。
 *
 * 这里全是纯函数 / 纯计算，不碰 DOM，因此可以直接用 `node --test` 测。
 */

/**
 * 可选格宽（秒），**升序**排列，缺省由后端按数据跨度自适应。
 *
 * 下限 60 秒：`/api/density` 的 `bucket_s` 是 `ge=1, le=86400`，60/180 本来合法，
 * 但比 1 分钟更细的档位没有意义 —— 一格都装不下几条，柱子只会全是 0/1。
 */
export const BUCKET_CHOICES = [
  { value: 60, label: '1 分钟' },
  { value: 180, label: '3 分钟' },
  { value: 300, label: '5 分钟' },
  { value: 600, label: '10 分钟' },
  { value: 900, label: '15 分钟' },
]

/** 每格最小像素宽度（可滚动模式下）。14px 是"还能点得中、也看得出高低"的下限。 */
export const MIN_SLOT_PX = 14

/**
 * 超过这么多格就**横向可滚动**。
 *
 * 与 `DENSE_BARS`（放弃保底宽度、等比压扁）配合使用：
 *   * `<= DENSE_BARS`：铺满容器，每格自适应宽度；
 *   * `DENSE_BARS < n <= SCROLL_BARS`：等比压扁（格子很小但**都在视野内**）；
 *   * `> SCROLL_BARS`：给每格一个可点宽度的下限，让整体可横向滚动 ——
 *     把几百格压成 1px 是没法用的（既看不清也点不准），滚动才是正确的退路。
 */
export const SCROLL_BARS = 600

/**
 * 桶数过多时切成"密集"渲染。
 *
 * 每格都有保底宽度（为了让空格子也点得中），但整库按 1 分钟聚合会有成千个桶，
 * 保底总宽一定超出面板 —— 多出来的格子被盖住，用户既看不到也点不到。
 * 阈值 400：后端自适应档的目标约 240 桶（`estimate_bucket_s(target=240)`），
 * 留出余量后自适应结果不会被误判成密集。
 */
export const DENSE_BARS = 400

function pad2(value) {
  return String(value).padStart(2, '0')
}

/** `13:10:00` */
export function formatStamp(ts) {
  const date = new Date(Number(ts) * 1000)
  return `${pad2(date.getHours())}:${pad2(date.getMinutes())}:${pad2(date.getSeconds())}`
}

/** `9/21 13:10` —— 概要行用：跨天的库只显示时分是看不出哪天的。 */
export function formatDay(ts) {
  const date = new Date(Number(ts) * 1000)
  return `${date.getMonth() + 1}/${date.getDate()} ${pad2(date.getHours())}:${pad2(date.getMinutes())}`
}

/** 该格的**时间区间**（`13:10:00–13:15:00`），悬停时比只给起点清楚得多。 */
export function formatSpan(ts, spanS) {
  return `${formatStamp(ts)}–${formatStamp(Number(ts) + Number(spanS))}`
}

/** 当前格宽（秒）：优先用响应里的实际值，缺省 60。 */
export function spanOf(density) {
  return Number(density?.bucket_s ?? 60)
}

/**
 * 概要行。
 *
 * 刻意**只放三件互相独立的事**：统计区间、每格时长、总量。
 * 早期版本把"格数 / 平均条数"也塞进同一行，还有 `formatDay` 打头的长文本，
 * 结果这一行又长又密，反而看不出哪一个数字重要（需求方反馈"信息较为混乱"）。
 * 格数挪到右侧的滚动提示里，平均条数直接不要 —— 它和总量是同一个信息的两种说法。
 */
export function buildSummary(density) {
  if (!density) return '加载中…'
  const lo = density.range?.lo
  const hi = density.range?.hi
  const spanS = spanOf(density)
  const spanText =
    spanS >= 60
      ? `${spanS % 60 === 0 ? spanS / 60 : (spanS / 60).toFixed(1)} 分钟`
      : `${spanS} 秒`
  if (lo == null || hi == null) return '该区间内没有数据'
  return `${formatDay(lo)} → ${formatDay(hi)}　每格 ${spanText}　共 ${density.total} 条`
}

/**
 * 归一化用的峰值。
 *
 * 用 **P95** 而不是最大值：实时弹幕的毛刺（一次涌入几百条）会把其余全压平。
 */
export function peakOf(buckets) {
  const counts = (buckets ?? []).map((bucket) => bucket.count).sort((a, b) => a - b)
  if (!counts.length) return 1
  const p95 = counts[Math.min(counts.length - 1, Math.floor(counts.length * 0.95))]
  return Math.max(p95, 1)
}

/**
 * 把 `/api/density` 的桶列表变成绘制用的柱子数组。
 *
 * `ratio` 是**高度比例**（0–1）。注意"有数据就至少留 4% 高度"这条在下游使用时
 * 仍然要判 `count > 0`：稀疏区整段消失会看起来像没有采集。
 */
export function buildBars(density) {
  const buckets = density?.buckets ?? []
  if (!buckets.length) return []
  const peak = peakOf(buckets)
  const spanS = spanOf(density)
  return buckets.map((bucket) => ({
    ts: bucket.ts,
    count: bucket.count,
    ratio: Math.min(1, bucket.count / peak),
    label: formatStamp(bucket.ts),
    span: formatSpan(bucket.ts, spanS),
    byType: bucket.by_type,
  }))
}

/**
 * 焦点格的下标（`-1` = 没有焦点）。
 *
 * 用**格子的区间**判定（`ts <= focus < ts + span`）而不是"最近的中心点"：
 * 点在第 3 格时，离第 3 格起点最近的桶就是第 3 格；但当 `focusTs` 恰好落在
 * 两格边界附近时，取最近会偶发偏到邻格 —— 区间判定没有这个问题。
 */
export function focusIndexOf(bars, focusTs, spanS) {
  if (focusTs == null || !bars?.length) return -1
  const ts = Number(focusTs)
  const width = Number(spanS)
  let best = -1
  let bestDelta = Infinity
  bars.forEach((bar, index) => {
    if (ts >= bar.ts && ts < bar.ts + width) {
      best = index
      bestDelta = 0
      return
    }
    if (bestDelta === 0) return
    // 兜底：focusTs 落在没有数据的空档里时，取时间上最近的一格
    const delta = Math.abs(bar.ts - ts)
    if (delta < bestDelta) {
      bestDelta = delta
      best = index
    }
  })
  return best
}

/** 无 focus 时按窗口区间高亮（"我在看哪一段"）。 */
export function inWindow(bar, { focusTs, range, spanS } = {}) {
  if (focusTs != null || !range || !bar) return false
  return bar.ts + Number(spanS) > range.lo && bar.ts <= range.hi
}

/**
 * 每格宽度与间隙（px）。
 *
 * 三种情形（与 `DENSE_BARS` / `SCROLL_BARS` 的说明对应）：
 *   * 可滚动：每格 `MIN_SLOT_PX` + 1px 间隙，总宽由 `trackWidth()` 给出；
 *   * 密集但不需要滚动：**没有间隙**，整宽平分（格子小到 1px 也不能溢出）；
 *   * 常规：留 1px 间隙，整宽平分。
 *
 * 返回 0 宽度时调用方应当跳过绘制（容器还没量到宽度）。
 */
export function slotGeometry({ count, viewportWidth, scrollable }) {
  if (!count || !viewportWidth) return { slot: 0, gap: 0 }
  if (scrollable) return { slot: MIN_SLOT_PX, gap: 1 }
  if (count > DENSE_BARS) return { slot: viewportWidth / count, gap: 0 }
  // 常规档也要保证"间隙之后还剩得下"：极端窄容器里 gap 会吃掉整格
  const gap = viewportWidth / count > 2 ? 1 : 0
  return { slot: Math.max((viewportWidth - gap * (count - 1)) / count, 0.5), gap }
}

/** 可滚动时的内容总宽（`slot + gap` 逐格累加）；不可滚动时就是容器宽。 */
export function trackWidth({ count, viewportWidth, scrollable }) {
  if (!scrollable) return viewportWidth
  return count * (MIN_SLOT_PX + 1)
}

/**
 * 像素坐标 → 格子下标（命中测试）。
 *
 * `x` 是**相对内容起点**的像素（即已经加上 `scrollLeft`）。
 * 返回 `null` 表示这一下没命中任何格子（点在内容右侧的空白里）。
 *
 * 刻意支持"点在空档里"：空格子也是合法的跳转目标（跳到那段没弹幕的时间）。
 * 但**超出内容范围**不算命中 —— 否则点最右边会莫名其妙跳到最后一格。
 */
export function hitTest({ x, count, slot, gap }) {
  if (!count || !slot || x < 0) return null
  const pitch = slot + gap
  const index = Math.floor(x / pitch)
  if (index < 0 || index >= count) return null
  return index
}

/**
 * 滚轮 → 横向滚动的位移；`0` 表示**不该拦截**这次滚轮。
 *
 * 时间轴只能在水平方向移动，所以竖直滚轮的 `deltaY` 直接当成水平位移
 * （与常见的"按住 Shift 才横滚"相比，这里不需要修饰键：鼠标停在一条窄窄的
 * 时间轴上时，用户意图几乎不可能是"滚动页面"）。
 *
 * 已经滚到两端就返回 0 让事件放行，否则用户会以为页面卡住了。
 */
export function wheelDelta({ deltaY, deltaX, scrollLeft, maxScroll }) {
  if (maxScroll <= 0) return 0
  const delta = deltaY !== 0 ? deltaY : deltaX
  if (!delta) return 0
  if (scrollLeft <= 0 && delta < 0) return 0
  if (scrollLeft >= maxScroll - 1 && delta > 0) return 0
  return delta
}
