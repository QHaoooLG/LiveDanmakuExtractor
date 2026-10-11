<script setup>
/**
 * 区间曲线（Phase 3，**方案 A，已是默认视图**）。
 *
 * ## 与 canvas 版的区别（为什么它更不容易错）
 *
 * canvas 版是"每格一个矩形 + 自己算命中 + 自绘滚动条 + sticky 吸附只画可视区"：
 * 绘制用内容坐标、命中用视口坐标，**两套各自推导**，吸附一失效就整体偏格
 * （2026-10-08 那个"点第 N 格亮第 N+48 格"就是这么来的）。
 *
 * 这一版把三件事一次性去掉：
 *
 * 1. **不需要滚动** —— 整条曲线自适应铺满宽度，于是没有 `scrollLeft`，
 *    也就不存在"绘制偏移 vs 命中偏移"这对孪生变量；
 * 2. **不需要画布** —— 一条折线 + 一个矩形 + 一个三角，SVG 三个节点就够，
 *    没有 DPR / `getContext` 可能为 null / 只画可视区那些分支；
 * 3. **不需要自绘滚动条** —— 缩放靠滚轮与窗口带把手，时间轴不再"横向滚一条尺"。
 *
 * ## 状态仍然是**外部**的（Phase 1 的成果）
 *
 * 这里**不持有**"在看哪一段"：`selection`（当前窗口）与 `spanS` 都是 props，
 * 手势只 `emit` 一个 `{ ts, spanS }`，由 `useHistory.jumpTo` 交给 reducer。
 * 组件里没有第二份真相 —— 这正是 Phase 1 换来的东西。
 *
 * ## 图层顺序（从下到上，画错会挡手势）
 *
 * 1. **`loaded`（列表已加载范围）**：`loadedRange` 只作**信息**展示 ——
 *    它是"列表这一页覆盖到哪"，**不是用户选的那一段**，所以画得最淡、
 *    用点线边框，并且**不参与任何手势**（以前踩过的教训：
 *    把派生量当控件用，会让"我在看哪一段"变成两个互相漂移的值）；
 * 2. **`band`（窗口带）**：用户选的那一段，带两端把手；
 * 3. 面积 + 折线；
 * 4. 播放头 + 刻度。
 *
 * ## 交互（每条都只产生一个"目标时刻 + 段长"）
 *
 * | 手势 | 行为 |
 * |------|------|
 * | 在曲线上按下并拖动 | 移动播放头（实时预览），松手时提交 |
 * | 拖窗口带两端把手 | 改段长（另一端固定） |
 * | 拖窗口带中间 | 整体平移（段长不变） |
 * | 单击曲线 | 定位到那一刻 |
 * | 滚轮 | 以光标为中心缩放（落到 1/3/5/10/15 分钟档位） |
 * | 方向键 / Home / End | 以格宽平移播放头 / 跳到两端 |
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  CLICK_SLOP,
  TOP_PAD,
  curveDomain,
  curvePath,
  densifyBuckets,
  dragAnchor,
  dragEdge,
  dragWindow,
  hitTest,
  timeTicks,
  toViewBox,
  wheelZoom,
  xOfTs,
} from '../curveGeometry.js'
import { BUCKET_CHOICES, buildSummary, formatStamp, formatTick, peakOf, spanOf } from '../timeline.js'
import DensityHead from './DensityHead.vue'

const props = defineProps({
  /** `/api/density` 的响应 */
  density: { type: Object, default: null },
  /** 当前历史窗口 `{ lo, hi }`（就是"我在看哪一段"的唯一真相） */
  selection: { type: Object, default: null },
  /** 正在定位的时间点（= 窗口锚点） */
  focusTs: { type: Number, default: null },
  /** 当前段长（秒）；缺省用密度的格宽 */
  spanS: { type: Number, default: null },
  /** **列表已加载**的时间范围（只读信息层，见上面的图层说明） */
  loadedRange: { type: Object, default: null },
  /** 曲线高度（px） */
  height: { type: Number, default: 46 },
  /** 当前视图（只用来决定表头那个开关显示什么） */
  view: { type: String, default: 'curve' },
})

const emit = defineEmits(['jump', 'update:bucket', 'update:view'])

const svg = ref(null)
/** 实测宽度（viewBox 用同一套单位，所以这里就是 viewBox 宽） */
const width = ref(0)

const buckets = computed(() => props.density?.buckets ?? [])
const bucketS = computed(() => spanOf(props.density))
const summary = computed(() => buildSummary(props.density))
/**
 * 当前窗口。**必须有一个**（即使还没进历史浏览）：它是窗口带的几何来源，
 * 也决定"滚轮缩放时从哪个段长出发"。右栏进入面板时就会把窗口初始化成
 * "数据末尾的一段"，所以这里通常拿得到；真的拿不到时
 * （窗口非法）就只画曲线，不画带子。
 */
const currentWindow = computed(() => {
  const lo = Number(props.selection?.lo)
  const hi = Number(props.selection?.hi)
  if (Number.isFinite(lo) && Number.isFinite(hi) && hi > lo) return { lo, hi }
  return null
})

const domain = computed(() => curveDomain(props.density?.range, currentWindow.value, bucketS.value))
/**
 * 绘制用的序列 = **补零后的**桶。
 *
 * 后端只返回"有数据的桶"，中间的空档要在这里补成 0：否则"没人说话的一小时"
 * 会被画成一条斜坡。补零范围按数据范围对齐到格边界。
 */
const series = computed(() => densifyBuckets(buckets.value, {
  bucketS: bucketS.value,
  range: props.density?.range,
}))
const peak = computed(() => peakOf(series.value))
const path = computed(() => curvePath(series.value, {
  domain: domain.value,
  width: width.value,
  height: props.height,
  peak: peak.value,
  bucketS: bucketS.value,
}))
const ticks = computed(() => timeTicks(domain.value, { width: width.value }))

/**
 * 诊断属性 `data-selection`：`lo;hi;spanS;anchorTs`。
 *
 * 探针与回归脚本靠它读"组件认为的窗口"，**不是调试残留**
 * （`tools/probe_curve_click.py` 直接解析它做一致性判定）。
 */
const selectionSnapshot = computed(() => {
  const window_ = currentWindow.value
  if (!window_) return ''
  const span = Number(props.spanS) || bucketS.value
  return `${window_.lo};${window_.hi};${Number.isFinite(span) ? span : ''};${props.focusTs ?? ''}`
})

/**
 * "列表已加载"淡色层。
 *
 * `loadedRange` 与轴（=`curveDomain`，以**数据范围**为准）不是一个东西：
 * 历史列表那一页可能只覆盖数据的一小段，也可能比屏幕上的这一段长得多。
 * 这里只把它**夹到画布内**画一条淡淡的提示，超出的部分不画 ——
 * 它的作用是"告诉你列表现在能看到哪儿"，不是"让你拖它"。
 */
const loadedRect = computed(() => {
  const lo = Number(props.loadedRange?.lo)
  const hi = Number(props.loadedRange?.hi)
  if (!width.value || !Number.isFinite(lo) || !Number.isFinite(hi) || hi < lo) return null
  const x1 = xOfTs(lo, { domain: domain.value, width: width.value })
  const x2 = xOfTs(hi, { domain: domain.value, width: width.value })
  const left = Math.min(Math.max(Math.min(x1, x2), 0), width.value)
  const right = Math.min(Math.max(Math.max(x1, x2), 0), width.value)
  if (right - left < 0.5) return null
  return { x: left, width: right - left }
})

/** 播放头的 x（没有锚点时不画） */
const playheadX = computed(() => {
  if (props.focusTs == null || !width.value) return null
  return xOfTs(props.focusTs, { domain: domain.value, width: width.value })
})

/** 窗口带的矩形（`x/width`，viewBox 坐标）—— 两端都夹进画布 */
const bandRect = computed(() => {
  const window_ = currentWindow.value
  if (!window_ || !width.value) return null
  const x1 = xOfTs(window_.lo, { domain: domain.value, width: width.value })
  const x2 = xOfTs(window_.hi, { domain: domain.value, width: width.value })
  const left = Math.min(Math.max(Math.min(x1, x2), 0), width.value)
  const right = Math.min(Math.max(Math.max(x1, x2), 0), width.value)
  return { x: left, width: Math.max(1, right - left) }
})

/**
 * 视图尺寸。
 *
 * ⚠️ **必须用 `getBoundingClientRect()`，不能用 `clientWidth`**：
 * `<svg>` 不是 HTML 元素，**没有 `clientWidth`**（jsdom 里恒为 `undefined`，
 * 于是宽度永远是 0、曲线永远不画，而在真浏览器里恰好被 `SVGSVGElement` 的实现
 * 兜住了 —— 这种"只在测试环境坏"的写法最难查）。
 *
 * `rect.width` 就是 CSS 像素宽，而我们让 viewBox 宽 == 它，于是
 * **1 个 viewBox 单位 = 1 个 CSS 像素**，手势换算不需要再乘缩放比。
 * 这一步是唯一把屏幕尺寸引进计算的地方。
 */
function measure() {
  const el = svg.value
  if (!el) return
  const rectWidth = el.getBoundingClientRect?.().width ?? 0
  if (rectWidth > 0) width.value = rectWidth
  else if (!width.value) width.value = el.parentElement?.clientWidth ?? 0
}

function viewPoint(event) {
  const rect = svg.value?.getBoundingClientRect?.()
  if (!rect) return null
  return toViewBox(event.clientX, event.clientY, rect, {
    width: width.value || rect.width,
    height: props.height,
  })
}

// -- 手势 ---------------------------------------------------------------- //

/** 当前拖的是什么：`curve`（播放头）/ `handle`（窗口带两端）/ `window`（整带） */
const drag = ref(null)
/** 当前段长：优先用外部给的 `spanS`，否则用密度的格宽 */
const span = computed(() => Number(props.spanS) || bucketS.value)

function onPointerDown(event) {
  if (!width.value) return
  const point = viewPoint(event)
  if (!point) return
  const target = hitTest(point.x, currentWindow.value, {
    domain: domain.value,
    width: width.value,
  })
  drag.value = {
    kind: target.kind,
    edge: target.edge,
    startX: point.x,
    startWindow: currentWindow.value,
    moved: false,
  }
  // 指针捕获是**可选**的：它保证鼠标拖出组件甚至拖出窗口也继续跟手。
  // 但它会抛（`NotFoundError`：pointerId 不是"活动指针"）—— jsdom 里必抛，
  // 真实浏览器在合成事件/触控笔等场景也可能抛。抛了就不捕获，拖动照样能用。
  try {
    event.target.setPointerCapture?.(event.pointerId)
  } catch {
    /* 拿不到捕获不影响拖动 */
  }
  // 按在曲线上时**立即**跟随（不用等移动），这样"点一下"也有即时反馈
  if (target.kind === 'curve') {
    emit('jump', { ts: dragAnchor(point.x, { domain: domain.value, width: width.value }), spanS: span.value })
  }
}

function onPointerMove(event) {
  const state = drag.value
  if (!state || !width.value) return
  const point = viewPoint(event)
  if (!point) return
  if (Math.abs(point.x - state.startX) > CLICK_SLOP) state.moved = true

  if (state.kind === 'curve') {
    emit('jump', { ts: dragAnchor(point.x, { domain: domain.value, width: width.value }), spanS: span.value })
    return
  }
  if (state.kind === 'handle') {
    const next = dragEdge(state.edge, point.x, state.startWindow, {
      domain: domain.value,
      width: width.value,
      minSpanS: bucketS.value,
    })
    const lo = Math.min(next.lo, next.hi)
    const hi = Math.max(next.lo, next.hi)
    emit('jump', { ts: (lo + hi) / 2, spanS: hi - lo })
    return
  }
  if (state.kind === 'window') {
    const deltaTs = point.x - state.startX === 0
      ? 0
      : (point.x - state.startX) / width.value * (domain.value.hi - domain.value.lo)
    const next = dragWindow(deltaTs, state.startWindow, { domain: domain.value })
    emit('jump', { ts: (next.lo + next.hi) / 2, spanS: next.hi - next.lo })
  }
}

function onPointerUp(event) {
  if (!drag.value) return
  drag.value = null
  try {
    event.target.releasePointerCapture?.(event.pointerId)
  } catch {
    /* 没捕获成功时 release 也会抛，忽略即可 */
  }
}

/**
 * 滚轮 → 以光标为中心缩放。
 *
 * `deltaY < 0`（向上滚）放大（段长变小）；粒度落到 `BUCKET_CHOICES` 的档位上，
 * 这样曲线与"每格 X 分钟"的语义保持一致。
 */
function onWheel(event) {
  if (!width.value) return
  const point = viewPoint(event)
  if (!point) return
  event.preventDefault()
  const factor = event.deltaY < 0 ? 1.6 : 1 / 1.6
  const spans = BUCKET_CHOICES.map((choice) => choice.value)
  const target = wheelZoom(point.x, factor, currentWindow.value, {
    domain: domain.value,
    width: width.value,
    minSpanS: spans[0],
    maxSpanS: spans[spans.length - 1],
  })
  // 吸到最近的档位（缩放要可预期，不能停在一个"每格 3.7 分钟"的怪值上）
  const spanS = spans.reduce(
    (best, value) => (Math.abs(value - target.spanS) < Math.abs(best - target.spanS) ? value : best),
    spans[0],
  )
  emit('jump', { ts: target.anchorTs, spanS })
}

/** 方向键平移播放头 / Home / End 跳到两端 */
function onKeydown(event) {
  const step = bucketS.value
  const anchor = props.focusTs ?? domain.value.lo
  const moves = {
    ArrowLeft: anchor - step,
    ArrowRight: anchor + step,
    Home: domain.value.lo,
    End: domain.value.hi,
  }
  if (!(event.key in moves)) return
  event.preventDefault()
  const ts = Math.min(Math.max(moves[event.key], domain.value.lo), domain.value.hi)
  emit('jump', { ts, spanS: span.value })
}

function pickBucket(value) {
  emit('update:bucket', Number(value))
}

defineExpose({
  /**
   * 测试钩子：重新量宽度。
   *
   * jsdom 不做布局，`clientWidth` 恒为 0，而组件在宽度为 0 时故意不画曲线。
   * 生产环境不需要它（浏览器里由 `ResizeObserver` 触发）。
   */
  refresh() {
    measure()
  },
})

let observer = null
onMounted(() => {
  measure()
  if (typeof ResizeObserver !== 'undefined' && svg.value) {
    observer = new ResizeObserver(measure)
    observer.observe(svg.value)
  } else if (typeof window !== 'undefined') {
    window.addEventListener('resize', measure)
  }
})
onBeforeUnmount(() => {
  observer?.disconnect()
  observer = null
  if (typeof window !== 'undefined') window.removeEventListener('resize', measure)
})
</script>

<template>
  <div
    class="curve"
    :data-focus="focusTs ?? ''"
    :data-curve-width="width"
    :data-selection="selectionSnapshot"
  >
    <DensityHead
      :density="density"
      :summary="summary"
      :focus-ts="focusTs"
      :count="series.length"
      :scrollable="false"
      @update:bucket="pickBucket"
    >
      <template #switch>
        <button
          class="mini switch"
          type="button"
          title="改回柱状时间轴（**回滚用**：曲线是当前默认视图）"
          @click="emit('update:view', 'canvas')"
        >
          改用柱状
        </button>
      </template>
    </DensityHead>

    <!-- 曲线本体。tabindex + keydown 让它可用键盘操作（方向键平移播放头） -->
    <svg
      ref="svg"
      class="curve-svg"
      :viewBox="`0 0 ${width || 1} ${height}`"
      :style="{ height: `${height + TOP_PAD}px` }"
      role="slider"
      tabindex="0"
      :aria-valuemin="Math.round(domain.lo)"
      :aria-valuemax="Math.round(domain.hi)"
      :aria-valuenow="focusTs ?? Math.round(domain.lo)"
      aria-label="时间轴：方向键平移播放头，滚轮缩放，拖动窗口两端调整时长"
      @pointerdown="onPointerDown"
      @pointermove="onPointerMove"
      @pointerup="onPointerUp"
      @pointercancel="onPointerUp"
      @wheel="onWheel"
      @keydown="onKeydown"
    >
      <!-- 「列表已加载」淡色层：**只读信息**（不参与手势），画在最底下。
           它比窗口带淡得多、且用点线边框，一眼看出它是信息不是控件。 -->
      <rect
        v-if="loadedRect"
        class="loaded"
        :x="loadedRect.x"
        :y="0"
        :width="loadedRect.width"
        :height="height"
      >
        <title>列表已加载的范围（{{ formatStamp(Number(loadedRange.lo)) }}–{{ formatStamp(Number(loadedRange.hi)) }}）</title>
      </rect>

      <!-- 当前窗口带：压在曲线下面（曲线是主角） -->
      <rect
        v-if="bandRect"
        class="band"
        :x="bandRect.x"
        :y="0"
        :width="bandRect.width"
        :height="height"
      />
      <!-- 面积 + 折线 -->
      <path v-if="path.area" class="area" :d="path.area" />
      <path v-if="path.line" class="line" :d="path.line" />

      <!-- 窗口带两端的把手（细竖线，命中靠 hitTest 的 handlePx） -->
      <template v-if="bandRect">
        <line class="handle" :x1="bandRect.x" :x2="bandRect.x" :y1="0" :y2="height" />
        <line
          class="handle"
          :x1="bandRect.x + bandRect.width"
          :x2="bandRect.x + bandRect.width"
          :y1="0"
          :y2="height"
        />
      </template>

      <!-- 播放头：一条竖线 + 顶部三角（"我在看这一刻"） -->
      <template v-if="playheadX != null">
        <line class="playhead" :x1="playheadX" :x2="playheadX" :y1="0" :y2="height" />
        <path class="playhead-cap" :d="`M${playheadX - 5},0 L${playheadX + 5},0 L${playheadX},7 Z`" />
      </template>

      <!-- 时间刻度：跨天的轴必须带日期，否则每天同一时刻的标签完全一样（实测 8 个 08:00:00） -->
      <text
        v-for="tick in ticks"
        :key="tick.ts"
        class="tick"
        :x="Math.min(Math.max(tick.x, 14), Math.max(width - 30, 14))"
        :y="height - 2"
      >
        {{ formatTick(tick.ts, domain.hi - domain.lo) }}
      </text>
    </svg>

    <p v-if="!buckets.length" class="empty-hint">暂无密度数据</p>
    <p v-else class="curve-hint">
      拖动播放头选时段 · 拖窗口两端调时长 · 滚轮缩放
      <span v-if="loadedRect" class="loaded-hint">· 淡色区＝列表已加载的范围（只读）</span>
    </p>
  </div>
</template>

<style scoped>
.curve {
  border-top: 1px solid var(--border);
  background: var(--panel);
  padding: 6px 14px 8px;
  min-width: 0;
  overflow: hidden;
}
.curve-svg {
  display: block;
  width: 100%;
  cursor: crosshair;
  touch-action: none; /* 让 pointermove 在触屏上不被滚动抢走 */
  outline: none;
}
.curve-svg:focus-visible { box-shadow: 0 0 0 2px #1e4d63; }
/* 曲线与窗口带：颜色沿用 canvas 版的一套，切视图时观感一致 */
.area { fill: #3f6d8e; fill-opacity: 0.28; }
.line { fill: none; stroke: #7dd3fc; stroke-width: 1; }
.band { fill: #1d2b36; fill-opacity: 0.55; }
/* 「列表已加载」淡色层：比窗口带**淡得多**、且用点线边框，一眼看出它是信息不是控件 */
.loaded {
  fill: #7dd3fc;
  fill-opacity: 0.08;
  stroke: #7dd3fc;
  stroke-opacity: 0.35;
  stroke-width: 1;
  stroke-dasharray: 2 3;
}
.loaded-hint { color: #6b7c8c; }
.handle { stroke: #7dd3fc; stroke-width: 1; stroke-dasharray: 3 2; }
.playhead { stroke: #f472b6; stroke-width: 1.5; }
.playhead-cap { fill: #f472b6; }
.tick { fill: #8b93a1; font-size: 9px; font-variant-numeric: tabular-nums; }
.curve-hint { margin: 2px 0 0; color: var(--muted); font-size: 11px; }
.empty-hint { margin: 2px 0 0; color: var(--muted); font-size: 12px; }
.mini.switch { color: var(--muted); }
</style>
