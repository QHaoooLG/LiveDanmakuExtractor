<script setup>
/**
 * 时间轴密度条 —— **canvas 版**（优化项 ⑩）。
 *
 * ## 为什么要换 canvas
 *
 * DOM 版（`DensityDom.vue`，**保留以便回滚**）每个桶渲染一个 `<button>`。
 * 常规档约 240 个桶还行，但整库按 1 分钟切会有几千个 —— 那是几千个 DOM 节点、
 * 每个都带 `title`，Vue 每次统计刷新都要 diff 一遍。canvas 只有**一个**节点，
 * 而且可以只画可视区。
 *
 * ## 与 DOM 版的关系（改之前先读）
 *
 * 只负责**画**和**把鼠标位置换算成桶**；所有数值（高度比例、焦点下标、概要行、
 * 每格宽度、命中测试、滚轮位移）都来自 `src/timeline.js`，表头来自
 * `DensityHead.vue` —— 两个渲染方式共用同一份计算与同一份文案，否则用户切一次
 * 渲染方式就会看到不同的数字。
 *
 * ## 三个容易踩的点
 *
 * 1. **`getContext('2d')` 可能是 `null`**（jsdom 没装 canvas、极老的浏览器）。
 *    这时**只是不画图**，交互与其它逻辑照常 —— 直接 `ctx.clearRect` 会抛异常，
 *    把整个面板带崩。
 * 2. **可滚动时 canvas 不做成全内容宽**：那样画布本身会有上万像素宽，白白吃内存。
 *    做法是外层撑出滚动条、canvas 只做可视区宽并吸附在左侧（`position: sticky`），
 *    滚动时按 `scrollLeft` 重画可视的那一段。
 * 3. **HiDPI**：只按 CSS 像素设 `width/height` 在高分屏上会糊，
 *    所以后备缓冲乘 `devicePixelRatio`，再用 `setTransform` 缩回去。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  DENSE_BARS,
  SCROLL_BARS,
  buildBars,
  buildSummary,
  focusIndexOf,
  hitTest,
  inWindow,
  slotGeometry,
  spanOf,
  trackWidth,
  wheelDelta,
} from '../timeline.js'
import DensityHead from './DensityHead.vue'

const props = defineProps({
  /** `/api/density` 的响应 */
  density: { type: Object, default: null },
  /** 当前历史窗口 `{ lo, hi }`（无 focus 时高亮这一段） */
  range: { type: Object, default: null },
  /** 正在定位的时间点（有值时只高亮这一格） */
  focusTs: { type: Number, default: null },
  /** 密度条高度（px） */
  height: { type: Number, default: 46 },
  /** 当前渲染方式：`canvas` / `dom`（只用来决定右上角那个开关怎么显示） */
  renderer: { type: String, default: 'canvas' },
})

const emit = defineEmits(['jump', 'update:bucket', 'update:renderer'])

const bars = computed(() => buildBars(props.density))
const span = computed(() => spanOf(props.density))
const summary = computed(() => buildSummary(props.density))
const dense = computed(() => bars.value.length > DENSE_BARS)
const scrollable = computed(() => bars.value.length > SCROLL_BARS)
const focusIndex = computed(() => focusIndexOf(bars.value, props.focusTs, span.value))

const scroller = ref(null)
const canvas = ref(null)
/** 可视区宽度（px），由 `ResizeObserver` 量出来 */
const viewportWidth = ref(0)
/** 当前横向滚动位置，重画时要用 */
const scrollLeft = ref(0)

const geometry = computed(() =>
  slotGeometry({
    count: bars.value.length,
    viewportWidth: viewportWidth.value,
    scrollable: scrollable.value,
  }),
)
const contentWidth = computed(() =>
  trackWidth({
    count: bars.value.length,
    viewportWidth: viewportWidth.value,
    scrollable: scrollable.value,
  }),
)

/**
 * 颜色从 CSS 变量读。
 *
 * canvas 拿不到 scoped 样式里的类名，所以只能读变量；读不到时用与
 * `style.css` 一致的硬编码兜底（测试环境没有样式表）。
 */
function themeColors() {
  const fallback = {
    slot: '#171a20',
    activeSlot: '#1d2b36',
    focusSlot: '#24384a',
    fill: '#3f6d8e',
    accent: '#7dd3fc',
  }
  const el = canvas.value
  if (!el || typeof window === 'undefined' || !window.getComputedStyle) return fallback
  const style = window.getComputedStyle(el)
  const read = (name, alt) => (style.getPropertyValue(name) || '').trim() || alt
  return {
    slot: fallback.slot,
    activeSlot: fallback.activeSlot,
    focusSlot: fallback.focusSlot,
    fill: fallback.fill,
    accent: read('--accent', fallback.accent),
  }
}

/**
 * 重画可视区。
 *
 * 只画 `[scrollLeft, scrollLeft + viewportWidth)` 这一段：可滚动时内容可能有
 * 上万像素，整幅画一遍纯属浪费（画布本身也不该有那么大）。
 */
function draw() {
  const el = canvas.value
  if (!el) return
  const list = bars.value
  const { slot, gap } = geometry.value
  const width = viewportWidth.value
  const height = props.height
  if (!width || !height) return

  const ctx = el.getContext?.('2d')
  // 拿不到 2d 上下文（jsdom / 极老浏览器）：**只是不画**，不要让面板崩掉
  if (!ctx) return

  const dpr = (typeof window !== 'undefined' && window.devicePixelRatio) || 1
  const bufferWidth = Math.max(Math.round(width * dpr), 1)
  const bufferHeight = Math.max(Math.round(height * dpr), 1)
  if (el.width !== bufferWidth || el.height !== bufferHeight) {
    el.width = bufferWidth
    el.height = bufferHeight
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  ctx.clearRect(0, 0, width, height)

  if (!list.length || !slot) return

  const colors = themeColors()
  const pitch = slot + gap
  const offset = scrollLeft.value
  // 算上 scrollLeft 才能只画可视区 —— 可滚动时内容可能有上万像素，
  // 整幅画一遍纯属浪费（画布本身也不该有那么大）。
  const startIndex = Math.max(0, Math.floor(offset / pitch))
  const endIndex = Math.min(list.length - 1, Math.ceil((offset + width) / pitch))
  const fillMax = Math.max(height - 1, 1)

  for (let index = startIndex; index <= endIndex; index += 1) {
    const bar = list[index]
    const x = index * pitch - offset
    // 背景格也要画：空格子同样可点（"跳到那段没弹幕的时间"）
    ctx.fillStyle =
      index === focusIndex.value
        ? colors.focusSlot
        : inWindow(bar, { focusTs: props.focusTs, range: props.range, spanS: span.value })
          ? colors.activeSlot
          : colors.slot
    ctx.fillRect(x, 0, Math.max(slot, 0.5), height)

    if (bar.count > 0) {
      // 有数据就至少留 4% 高度，否则稀疏区会整段消失、看起来像没有采集。
      // 注意下限用的是 `height` 而不是 `fillMax`：`fillMax` 比它小 1px，
      // 拿它当分母会让"4%"实际只有 3.9%，这种偏差不值得留。
      const barHeight = Math.max(bar.ratio * fillMax, height * 0.04)
      ctx.fillStyle = index === focusIndex.value ? colors.accent : colors.fill
      ctx.fillRect(x, height - barHeight, Math.max(slot, 0.5), barHeight)
    }
  }
}

/**
 * 鼠标位置 → 桶下标。
 *
 * `clientX` 要**加上 `scrollLeft`** 才是"相对内容起点"的坐标 ——
 * 这一条算错的话，滚动之后点击会跳错格（而且只在滚动后复现，最难查）。
 */
function indexAtEvent(event) {
  const el = scroller.value
  if (!el) return null
  const { slot, gap } = geometry.value
  const rect = el.getBoundingClientRect()
  const x = event.clientX - rect.left + (el.scrollLeft ?? 0)
  return hitTest({ x, count: bars.value.length, slot, gap })
}

const hovered = ref(null)
const hoverTitle = computed(() => {
  const bar = hovered.value
  if (!bar) return ''
  return `${bar.span}\n${bar.count} 条`
})

function onMouseMove(event) {
  const index = indexAtEvent(event)
  hovered.value = index == null ? null : bars.value[index]
}

function onMouseLeave() {
  hovered.value = null
}

function onClick(event) {
  const index = indexAtEvent(event)
  if (index == null) return
  const bar = bars.value[index]
  if (bar) emit('jump', { ts: bar.ts, bucketS: span.value })
}

/**
 * 滚轮 → 横向滚动。
 *
 * 位移量由 `timeline.wheelDelta()` 判定：返回 0 就**不拦截**，
 * 让事件放行（已经到两端时用户多半是想滚页面）。
 */
function onWheel(event) {
  const el = scroller.value
  if (!el || !scrollable.value) return
  const max = el.scrollWidth - el.clientWidth
  const delta = wheelDelta({
    deltaY: event.deltaY,
    deltaX: event.deltaX,
    scrollLeft: el.scrollLeft,
    maxScroll: max,
  })
  if (!delta) return
  event.preventDefault()
  el.scrollLeft += delta
}

function onScroll() {
  const el = scroller.value
  if (!el) return
  scrollLeft.value = el.scrollLeft ?? 0
}

function measure() {
  const el = scroller.value
  if (!el) return
  viewportWidth.value = el.clientWidth ?? 0
}

function pickBucket(value) {
  emit('update:bucket', Number(value))
}

let observer = null

/**
 * 暴露给测试的两个内部动作：**重新量宽度**与**重画**。
 *
 * 为什么必须开这个口子：jsdom 不做布局，`clientWidth` 恒为 0，而组件在宽度为 0 时
 * 故意不画。测试里只能"先设尺寸、再让它重量一次"，否则所有绘制断言都会在
 * "什么都没画"的前提下通过（那种测试比没有更糟）。
 *
 * 生产代码不需要它们 —— 浏览器里由 `ResizeObserver` 与 `watch` 负责触发。
 */
defineExpose({ measure, draw })

onMounted(() => {
  measure()
  onScroll()
  draw()
  // 面板宽度会随窗口/侧栏变化 —— 不重新量宽度的话柱子会被拉伸或留白
  if (typeof ResizeObserver !== 'undefined' && scroller.value) {
    observer = new ResizeObserver(measure)
    observer.observe(scroller.value)
  } else if (typeof window !== 'undefined') {
    window.addEventListener('resize', measure)
  }
})

onBeforeUnmount(() => {
  observer?.disconnect()
  observer = null
  if (typeof window !== 'undefined') window.removeEventListener('resize', measure)
})

// 影响绘制的输入一变就重画。用 watch 而不是 computed：
// 绘制是**副作用**，不该（也不能）出现在计算属性里。
watch(
  () => [
    props.density,
    props.range,
    props.focusTs,
    props.height,
    viewportWidth.value,
    scrollLeft.value,
  ],
  () => draw(),
)
</script>

<template>
  <div class="density">
    <DensityHead
      :density="density"
      :summary="summary"
      :focus-ts="focusTs"
      :count="bars.length"
      :scrollable="scrollable"
      @update:bucket="pickBucket"
    >
      <template #switch>
        <button
          class="mini switch"
          type="button"
          title="改用原来的 DOM 绘制（每格一个按钮，可用 Tab 键走；桶多时更耗资源）"
          @click="emit('update:renderer', 'dom')"
        >
          改用 DOM
        </button>
      </template>
    </DensityHead>
    <!-- 滚动容器：`track` 撑出滚动条，canvas 吸附在左侧只画可视区 -->
    <div
      ref="scroller"
      class="track-scroll"
      :class="{ scrollable }"
      :style="{ height: `${height}px` }"
      @wheel="onWheel"
      @scroll="onScroll"
      @mousemove="onMouseMove"
      @mouseleave="onMouseLeave"
      @click="onClick"
    >
      <div
        class="track"
        :class="{ dense }"
        :style="{ height: `${height}px`, width: `${contentWidth}px` }"
      >
        <canvas
          ref="canvas"
          class="track-canvas"
          :style="{ width: `${viewportWidth}px`, height: `${height}px` }"
          :title="hoverTitle"
        ></canvas>
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
.mini.switch { color: var(--muted); }

.track-scroll { overflow-x: auto; overflow-y: hidden; cursor: pointer; }
.track-scroll.scrollable { padding-bottom: 4px; }
.track-scroll::-webkit-scrollbar { height: 8px; }
.track-scroll::-webkit-scrollbar-thumb { background: #2c313a; border-radius: 4px; }
.track-scroll::-webkit-scrollbar-thumb:hover { background: #3a4048; }
.track { position: relative; }
/* 可滚动时把画布**吸附**在可视区左侧：它只有一屏宽，不 sticky 就会被滚出视野 */
.track-canvas { display: block; position: sticky; left: 0; }
.empty-hint { position: absolute; inset: 0; display: flex; align-items: center; color: var(--muted); font-size: 12px; }
</style>
