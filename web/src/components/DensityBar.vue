<script setup>
/**
 * 时间轴密度条（V3-a 的"定位"入口）。
 *
 * 数据来自 `/api/density`（服务端 SQL 聚合），**不是**把事件搬到前端再算：
 * 实测 20 万行、桶宽 60s 时，服务端聚合约 38~95 ms，而"取回事件再前端聚合"
 * 正是旧路径 `iter_events(limit=20100)` 在干的事（约 3.6 s）。
 *
 * 交互：
 *   * 点任意一根条 → 跳到那个时间段（`jump` 事件带上该格的**起止时刻**）；
 *   * 鼠标悬停显示该桶的时间与条数；
 *   * 底部可切换**每格时长**（1/3/5/10/15 分钟）—— 跨度大的库用粗粒度看趋势，
 *     想定位就用细粒度；
 *   * 格数超过 `SCROLL_BARS` 时，时间轴**横向可滚动**，鼠标滚轮在轴上即可左右滑动
 *     （不用按住 Shift，也不丢格子 —— 见下方 `onWheel`）。
 *
 * 高亮的语义（改之前先读）：
 *   * **有 `focusTs`（刚点了某一格）时只高亮那一格** —— 用户点哪格就该亮哪格，
 *     这是实测反馈过的 off-by-one 的直接要求；
 *   * 没有 `focusTs` 时高亮整个历史窗口覆盖的区间（"我正在看哪一段"）。
 */
import { computed, ref } from 'vue'

const props = defineProps({
  /** `/api/density` 的响应 */
  density: { type: Object, default: null },
  /** 当前历史窗口 `{ lo, hi }`（无 focus 时高亮这一段） */
  range: { type: Object, default: null },
  /** 正在定位的时间点（有值时只高亮这一格） */
  focusTs: { type: Number, default: null },
  /** 密度条高度（px） */
  height: { type: Number, default: 46 },
})

const emit = defineEmits(['jump', 'update:bucket'])

/**
 * 可选格宽（秒），**升序**排列，缺省由后端按数据跨度自适应。
 *
 * 下限 60 秒：`/api/density` 的 `bucket_s` 是 `ge=1, le=86400`，60/180 本来合法，
 * 但比 1 分钟更细的档位没有意义 —— 一格都装不下几条，柱子只会全是 0/1。
 */
const BUCKET_CHOICES = [
  { value: 60, label: '1 分钟' },
  { value: 180, label: '3 分钟' },
  { value: 300, label: '5 分钟' },
  { value: 600, label: '10 分钟' },
  { value: 900, label: '15 分钟' },
]

const buckets = computed(() => props.density?.buckets ?? [])

/** 用 P95 而不是最大值做归一化：实时弹幕的毛刺（一次涌入几百条）会把其余全压平。 */
const scale = computed(() => {
  const counts = buckets.value.map((bucket) => bucket.count).sort((a, b) => a - b)
  if (!counts.length) return 1
  const p95 = counts[Math.min(counts.length - 1, Math.floor(counts.length * 0.95))]
  return Math.max(p95, 1)
})

/**
 * 每格最小像素宽度（可滚动模式下）。14px 是"还能点得中、也看得出高低"的下限。
 */
const MIN_SLOT_PX = 14
/**
 * 超过这么多格就**横向可滚动**。
 *
 * 与 `DENSE_BARS`（放弃保底宽度、等比压扁）配合使用：
 *   * `<= DENSE_BARS`：铺满容器，每格自适应宽度；
 *   * `DENSE_BARS < n <= SCROLL_BARS`：等比压扁（格子很小但**都在视野内**）；
 *   * `> SCROLL_BARS`：给每格一个可点宽度的下限，让整体可横向滚动 ——
 *     把几百格压成 1px 是没法用的（既看不清也点不准），滚动才是正确的退路。
 */
const SCROLL_BARS = 600

const bars = computed(() => {
  const list = buckets.value
  if (!list.length) return []
  const peak = scale.value
  return list.map((bucket) => ({
    ts: bucket.ts,
    count: bucket.count,
    // 有数据就至少留 4% 高度，否则稀疏区会整段消失、看起来像没有采集
    ratio: Math.min(1, bucket.count / peak),
    label: formatStamp(bucket.ts),
    span: formatSpan(bucket.ts),
    byType: bucket.by_type,
  }))
})

/**
 * 桶数过多时切成"密集"渲染。
 *
 * 每格都有 `min-width: 2px` + `gap: 1px` 的保底宽度（为了让空格子也点得中），
 * 但整库按 1 分钟聚合会有成千个桶，保底总宽一定超出面板 —— 多出来的格子被右栏盖住，
 * 用户既看不到也点不到，而且界面上没有任何提示。超过阈值就放弃保底宽度，等比压扁。
 *
 * 阈值 400：后端自适应档的目标约 240 桶（`estimate_bucket_s(target=240)`），
 * 留出余量后自适应结果不会被误判成密集。
 */
const DENSE_BARS = 400
const dense = computed(() => bars.value.length > DENSE_BARS)
/** 格数多到压扁也不够看了 ⇒ 改成可横向滚动（每格给可点宽度） */
const scrollable = computed(() => bars.value.length > SCROLL_BARS)

/** 可滚动时的时间轴最小总宽（每格 `MIN_SLOT_PX`，含间隙）。 */
const trackMinWidth = computed(() =>
  scrollable.value ? `${bars.value.length * (MIN_SLOT_PX + 1)}px` : null,
)

const scroller = ref(null)

/**
 * 滚轮 → **横向滚动**。
 *
 * 时间轴只能在水平方向移动，所以竖直滚轮的 `deltaY` 直接当成水平位移
 * （与常见的"按住 Shift 才横滚"相比，这里不需要修饰键：鼠标停在一条窄窄的时间轴上时，
 * 用户意图几乎不可能是"滚动页面"）。
 *
 * 只有**真的能滚**时才 `preventDefault`：已经滚到两端就放行，
 * 否则用户会以为页面卡住了。
 */
function onWheel(event) {
  const el = scroller.value
  if (!el || !scrollable.value) return
  const max = el.scrollWidth - el.clientWidth
  if (max <= 0) return
  const delta = event.deltaY !== 0 ? event.deltaY : event.deltaX
  const atStart = el.scrollLeft <= 0 && delta < 0
  const atEnd = el.scrollLeft >= max - 1 && delta > 0
  if (atStart || atEnd) return
  event.preventDefault()
  el.scrollLeft += delta
}

function formatStamp(ts) {
  const date = new Date(ts * 1000)
  const pad = (n) => String(n).padStart(2, '0')
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

/** 该格的**时间区间**（`13:10:00–13:15:00`），悬停时比只给起点清楚得多。 */
function formatSpan(ts) {
  const spanS = Number(props.density?.bucket_s ?? 60)
  return `${formatStamp(ts)}–${formatStamp(Number(ts) + spanS)}`
}

function formatDay(ts) {
  const date = new Date(ts * 1000)
  const pad = (n) => String(n).padStart(2, '0')
  return `${date.getMonth() + 1}/${date.getDate()} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

/** 当前格宽（秒）：优先用响应里的实际值，缺省 60。 */
const span = computed(() => Number(props.density?.bucket_s ?? 60))

/**
 * 焦点格的下标。
 *
 * 用**格子的区间**判定（`ts <= focus < ts + span`）而不是"最近的中心点"：
 * 点在第 3 格时，离第 3 格起点最近的桶就是第 3 格；但当 `focusTs` 恰好落在
 * 两格边界附近时，取最近会偶发偏到邻格 —— 区间判定没有这个问题。
 */
const focusIndex = computed(() => {
  if (props.focusTs == null) return -1
  const ts = Number(props.focusTs)
  const spanS = span.value
  let best = -1
  let bestDelta = Infinity
  bars.value.forEach((bar, index) => {
    const inside = ts >= bar.ts && ts < bar.ts + spanS
    if (inside) {
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
})

/** 无 focus 时按窗口区间高亮（"我在看哪一段"）。 */
function inWindow(bar) {
  if (props.focusTs != null || !props.range) return false
  const spanS = span.value
  return bar.ts + spanS > props.range.lo && bar.ts <= props.range.hi
}

/**
 * 概要行。
 *
 * 刻意**只放三件互相独立的事**：统计区间、每格时长、总量。
 * 早期版本把"格数 / 平均条数"也塞进同一行，还有 `formatDay` 打头的长文本，
 * 结果这一行又长又密，反而看不出哪一个数字重要（需求方反馈"信息较为混乱"）。
 * 格数挪到右侧的滚动提示里，平均条数直接不要 —— 它和总量是同一个信息的两种说法。
 */
const summary = computed(() => {
  const payload = props.density
  if (!payload) return '加载中…'
  const lo = payload.range?.lo
  const hi = payload.range?.hi
  const spanS = Number(payload.bucket_s ?? 60)
  const spanText =
    spanS >= 60
      ? `${spanS % 60 === 0 ? spanS / 60 : (spanS / 60).toFixed(1)} 分钟`
      : `${spanS} 秒`
  if (lo == null || hi == null) return '该区间内没有数据'
  return `${formatDay(lo)} → ${formatDay(hi)}　每格 ${spanText}　共 ${payload.total} 条`
})

function onClick(index) {
  const bar = bars.value[index]
  if (bar) emit('jump', { ts: bar.ts, bucketS: span.value })
}

function pickBucket(value) {
  emit('update:bucket', Number(value))
}
</script>

<template>
  <div class="density">
    <div class="density-head">
      <span class="title">时间轴</span>
      <span class="summary">{{ summary }}</span>
      <span class="spacer"></span>
      <span class="bucket-label" title="每格时长越短，定位越精确；越长，越能看清整体趋势">每格</span>
      <button
        v-for="choice in BUCKET_CHOICES"
        :key="choice.value"
        class="mini"
        type="button"
        :class="{ on: Number(density?.bucket_s) === choice.value }"
        @click="pickBucket(choice.value)"
      >
        {{ choice.label }}
      </button>
      <span v-if="focusTs" class="focus">定位 {{ formatStamp(focusTs) }}</span>
      <span v-if="scrollable" class="scroll-hint" title="鼠标滚轮即可左右滑动时间轴">
        {{ bars.length }} 格 · 可横向滚动
      </span>
    </div>
    <!-- 可滚动时外面套一层滚动容器：`track` 用 min-width 撑开，格子保持可点宽度 -->
    <div
      ref="scroller"
      class="track-scroll"
      :class="{ scrollable }"
      :style="{ height: `${height}px` }"
      @wheel="onWheel"
    >
      <div
        class="track"
        :class="{ dense }"
        :style="{ height: `${height}px`, minWidth: trackMinWidth }"
      >
        <button
          v-for="(bar, index) in bars"
          :key="bar.ts"
          class="bar-slot"
          type="button"
          :class="{ active: inWindow(bar), focus: index === focusIndex }"
          :title="`${bar.span}\n${bar.count} 条`"
          @click="onClick(index)"
        >
          <span class="bar-fill" :style="{ height: `${Math.max(bar.ratio * 100, bar.count ? 4 : 0)}%` }"></span>
        </button>
        <div v-if="!bars.length" class="empty-hint">暂无密度数据</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.density {
  border-top: 1px solid var(--border);
  background: var(--panel);
  padding: 6px 14px 8px;
}
.density-head { display: flex; align-items: center; gap: 6px; font-size: 11px; margin-bottom: 4px; flex-wrap: wrap; }
.density-head .title { color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; font-weight: 600; }
.density-head .summary { color: var(--muted); }
.density-head .spacer { flex: 1; }
.density-head .bucket-label { color: var(--muted); margin-left: 4px; }
.density-head .focus { color: var(--accent); border: 1px solid #1e4d63; border-radius: 999px; padding: 0 8px; margin-left: 4px; }
.density-head .scroll-hint { color: var(--accent); border: 1px solid #1e4d63; border-radius: 999px; padding: 0 8px; margin-left: 4px; }
.mini { padding: 1px 8px; font-size: 11px; }
.mini.on { color: var(--bg); background: var(--accent); border-color: var(--accent); font-weight: 600; }

/* 滚动容器：只有格数多到压扁也没法用时才出现横向滚动条。
   `overflow-x:auto` 常驻也无害（`track` 不撑宽时不会有滚动条）。 */
.track-scroll { overflow-x: auto; overflow-y: hidden; }
.track-scroll.scrollable { padding-bottom: 4px; }
.track-scroll::-webkit-scrollbar { height: 8px; }
.track-scroll::-webkit-scrollbar-thumb { background: #2c313a; border-radius: 4px; }
.track-scroll::-webkit-scrollbar-thumb:hover { background: #3a4048; }
.track { position: relative; display: flex; align-items: flex-end; gap: 1px; }
/* 每格都是可点区域（哪怕该格没有数据），这样"跳到某个空档"也能定位 */
.bar-slot {
  flex: 1 1 0; min-width: 2px; height: 100%;
  display: flex; align-items: flex-end;
  padding: 0; border: none; border-radius: 1px;
  background: #171a20; cursor: pointer;
}
.bar-slot:hover { background: #20242c; }
/* 桶太多时（如整库按 1 分钟切）放弃保底宽度：宁可把它压扁，也不能让它溢出面板 */
.track.dense { gap: 0; }
.track.dense .bar-slot { min-width: 0; }
.bar-slot.active { background: #1d2b36; }
.bar-slot.focus { background: #24384a; box-shadow: inset 0 0 0 1px var(--accent); }
.bar-fill { display: block; width: 100%; background: #3f6d8e; border-radius: 1px 1px 0 0; }
.bar-slot.active .bar-fill { background: var(--accent); }
.bar-slot.focus .bar-fill { background: var(--accent); }
.bar-slot:hover .bar-fill { background: #6ba6cd; }
.empty-hint { position: absolute; inset: 0; display: flex; align-items: center; color: var(--muted); font-size: 12px; }
</style>
