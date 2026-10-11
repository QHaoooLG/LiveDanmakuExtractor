<script setup>
/**
 * 时间轴密度条 —— **canvas 版**（**Phase 2 之后唯一的稳定实现**）。
 *
 * ## 为什么是它留到现在
 *
 * Phase 2 把"canvas / DOM 两个渲染器"收敛成了一个：
 *
 * * **DOM 版（`DensityDom.vue`）已删除** —— 它每个桶渲染一个 `<button>`，
 *   整库按 1 分钟切会有几千个节点、每个都带 `title`，Vue 每次刷新都要 diff 一遍；
 *   更贵的是**维护税**：两个渲染器必须保持一模一样，当年得专门写用例断言
 *   "两版对同一格给出的文案一字不差"。留两个实现 = 每次改动都要改两遍、
 *   测两遍，而用户只看得见其中一个。
 * * 现在表头右侧的开关是 **canvas ↔ 曲线**（Phase 3 已转正为**默认**视图，
 *   这里保留一个版本周期做回滚，见 `DensityBar`），不再是 canvas ↔ DOM。
 *
 * ## 现在负责什么
 *
 * 只负责**画**和**把鼠标位置换算成桶**；所有数值（高度比例、焦点下标、概要行、
 * 每格宽度、命中测试、滚轮位移）都来自 `src/timeline.js`，表头来自
 * `DensityHead.vue`。**"我在看哪一段"由 `useTimelineWindow` 的唯一真相源决定**
 * （Phase 1）：这里只读 `focusTs`（= 窗口锚点），不自己推导。
 *
 * ## 三个容易踩的点
 *
 * 1. **`getContext('2d')` 可能是 `null`**（jsdom 没装 canvas、极老的浏览器）。
 *    这时**只是不画图**，交互与其它逻辑照常 —— 直接 `ctx.clearRect` 会抛异常，
 *    把整个面板带崩。
 * 2. **可滚动时 canvas 不做成全内容宽**：那样画布本身会有上万像素宽，白白吃内存。
 *    做法是外层撑出滚动条、canvas 只做可视区宽并吸附在左侧（`position: sticky`），
 *    滚动时按 `scrollLeft` 重画可视的那一段。
 *    ⚠️ **宽度必须由内联样式按像素给**（见 `canvasStyle`）：写成 CSS `width: 100%`
 *    会让它和 sticky 包含块 `.track` 等宽、吸附彻底失效。
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
  formatStamp,
  hitTest,
  inWindow,
  slotGeometry,
  spanOf,
  trackWidth,
  wheelDelta,
} from '../timeline.js'
import DensityHead from './DensityHead.vue'
import TimelineScrollbar from './TimelineScrollbar.vue'

const props = defineProps({
  /** `/api/density` 的响应 */
  density: { type: Object, default: null },
  /** 当前历史窗口 `{ lo, hi }`（无 focus 时高亮这一段） */
  /**
   * **当前选中的那段窗口**（`{lo, hi}`）—— 只有落在它里面的柱子才画成"窗口内"。
   *
   * 与曲线的 `selection` 同源（`useHistory.window`）。历史上这里收的是
   * "已加载历史页的时间范围"，那是派生量、会随弹幕密度漂（见 `TimelineCurve` 的说明）。
   */
  selection: { type: Object, default: null },
  /** 正在定位的时间点（有值时只高亮这一格） */
  focusTs: { type: Number, default: null },
  /** 密度条高度（px） */
  height: { type: Number, default: 46 },
  /** 当前视图（`canvas` / `curve`）；只用来决定表头那个开关显示什么 */
  view: { type: String, default: 'canvas' },
})

const emit = defineEmits(['jump', 'update:bucket', 'update:view'])

const bars = computed(() => buildBars(props.density))
const span = computed(() => spanOf(props.density))
const summary = computed(() => buildSummary(props.density))
const scrollable = computed(() => bars.value.length > SCROLL_BARS)
/**
 * "密集压扁"档：**只在不可滚动时才有意义**。
 *
 * 两个阈值都来自 `timeline.js`，且 `SCROLL_BARS > DENSE_BARS` —— 这里再显式加一句
 * `!scrollable`，是为了让"超过 60 格就走滚动"这条需求**与 DENSE_BARS 的取值无关**
 * （否则以后有人只调 DENSE_BARS 就会把滚动策略改掉）。
 */
const dense = computed(() => !scrollable.value && bars.value.length > DENSE_BARS)
const focusIndex = computed(() => focusIndexOf(bars.value, props.focusTs, span.value))

/**
 * 高亮格的**数值快照**，挂在 `.density` 的 `data-focus` 上（Phase 1 加的取证口子）。
 *
 * 格式：`focusIndex;count;pitch|barStart;barEnd`
 *
 * 为什么不用"读表头文案再反推"：文案只有秒级精度，反推出的格下标会落在边界附近，
 * 明明对了也可能判成偏一格（回归脚本就这么误判过一次）。这里给的是**画图那一刻
 * 真正用的下标**，以及它那一格的**时间区间** —— 判定"显示的时段与点击的时段是否
 * 匹配"应该比时间，而不是比像素（像素口径依赖脚本对 `pitch` 的假设，那个假设也错过）。
 */
const focusSnapshot = computed(() => {
  const list = bars.value
  const { slot, gap } = geometry.value
  const index = focusIndex.value
  const bar = index >= 0 ? list[index] : null
  const range = bar ? `${bar.ts};${Number(bar.ts) + span.value}` : ''
  return `${index};${list.length};${slot + gap}|${range}`
})

const scroller = ref(null)
const canvas = ref(null)
/** 自绘滚动条：量完宽度要让它重算轨道（见 `measure`） */
const scrollbarRef = ref(null)
/** 可视区宽度（px），由 `ResizeObserver` 量出来 */
const viewportWidth = ref(0)
/** 当前横向滚动位置，重画时要用 */
const scrollLeft = ref(0)

/**
 * "点击定格"期间要守住的横向偏移。
 *
 * `value = null` 表示不接管（用户没点过，或者已经交还给用户）。
 * `applied` 保证**只复原一次** —— 复原动作本身也会触发重画，不设这个标记会来回拽。
 */
const keepOffset = { value: null, at: 0, applied: false }

/** 点格子时的偏移最多接管这么久（毫秒）；之后交给用户。 */
const KEEP_OFFSET_MS = 2000

/** 滚动条两端的刻度（整段的首尾时间）——让人知道"我在整场的哪一头"。 */
const ticks = computed(() => {
  const list = bars.value
  if (!list.length || !scrollable.value) return []
  return [{ text: formatStamp(list[0].ts) }, { text: formatStamp(list[list.length - 1].ts) }]
})

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
 * 画布的**内联宽度**（px）。
 *
 * ⚠️ 必须写成**像素**，不能靠 CSS 的 `width: 100%` —— 这是"滑动之后点哪格就亮哪格"
 * 的前提，实测（真 Chrome，`tools/probe_timeline_sticky.py`）：
 *
 *   * `width: 100%` 时画布被解析成**整个内容宽**（122 格 × 15px = 1830px），
 *     而它 `position: sticky` 的包含块正是 `.track`（同样 1830px）——
 *     **sticky 只能在包含块内部挪动**，元素和包含块一样宽就没有任何挪动余地，
 *     于是画布跟着内容一起滚出视口（实测 `scrollLeft=718` 时 `canvas.left = -704`，
 *     而视口左边界是 `14`）；
 *   * 后果是**画出来的高亮与用户看到的格子错位**：`draw()` 只往画布最左边
 *     `viewportWidth` 像素里画（`x = index * pitch - scrollLeft`，x 是**内容坐标**），
 *     可用户看到的是"内容坐标 = scrollLeft 起"的那一段 —— 两者差一个 `scrollLeft`。
 *     点在第 N 格，亮的就是第 N + scrollLeft/pitch 格。
 *   * 内联成 `viewportWidth` 像素之后 sticky 立刻生效（同一次实测：`canvas.left = 14`），
 *     绘制坐标与命中坐标重新对齐。
 *
 * 还没量到宽度时给 `100%`（那时也不画），避免出现 `0px` 宽的画布。
 */
const canvasStyle = computed(() => ({
  height: `${props.height}px`,
  ...(viewportWidth.value ? { width: `${viewportWidth.value}px` } : {}),
}))

/**
 * 颜色从 CSS 变量读。
 *
 * canvas 拿不到 scoped 样式里的类名，所以只能读变量；读不到时用与
 * `style.css` 一致的硬编码兜底（测试环境没有样式表）。
 *
 * ## 为什么缓存
 *
 * `window.getComputedStyle()` 会**强制一次样式重算**（style flush），
 * 而 `draw()` 在**每个滚动像素**上都会被调用（`scrollLeft` 在 watch 源里）。
 * 每次重算样式 + 新建一个对象，在滚动时是纯浪费 ——
 * 颜色是主题级的，除非主题变了否则不会变。
 */
let colorCache = null

function themeColors() {
  if (colorCache) return colorCache
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
  colorCache = {
    slot: fallback.slot,
    activeSlot: fallback.activeSlot,
    focusSlot: fallback.focusSlot,
    fill: fallback.fill,
    accent: read('--accent', fallback.accent),
  }
  return colorCache
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
        : inWindow(bar, { focusTs: props.focusTs, range: props.selection, spanS: span.value })
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
  if (!bar) return
  // 记下"点击那一刻的横向偏移"：跳转触发的密度重算会把滚动容器打回 0，
  // 而那一格的内容坐标没变 —— 复原偏移才能让"亮起来的那一格"仍停在眼前。
  keepOffset.value = scroller.value?.scrollLeft ?? 0
  keepOffset.at = Date.now()
  keepOffset.applied = false
  emit('jump', { ts: bar.ts, bucketS: span.value })
}

/**
 * 点格子之后把横向偏移**放回原处**（见 `onClick` 里的说明）。
 *
 * 只在"点击之后很短的一段时间内"接管，而且**只在偏移确实被重置过**时才写回 ——
 * 用户自己在滑的时候硬拽回去，比"高亮跑出屏幕"更烦人。
 */
function restoreScrollIfNeeded() {
  if (keepOffset.value == null || keepOffset.applied) return
  const el = scroller.value
  if (!el) return
  if (Date.now() - keepOffset.at > KEEP_OFFSET_MS) {
    keepOffset.value = null
    return
  }
  const current = el.scrollLeft ?? 0
  if (Math.abs(current - keepOffset.value) > 4) {
    el.scrollLeft = keepOffset.value
    onScroll()
    draw()
  }
  keepOffset.applied = true
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

/**
 * 滚动条拖拽/点轨道/键盘事件 → 设置滚动容器的位置。
 *
 * 位置的**唯一真相是滚动容器**（`scroller.scrollLeft`）：滚动条是受控的，
 * 它只发目标值过来，由这里落到 DOM 上，`scroll` 事件再把真值传回去。
 * 这样就不会出现"滚动条和内容各说各话"。
 */
function scrollTo(offset) {
  const el = scroller.value
  if (!el) return
  el.scrollLeft = Math.max(0, Number(offset) || 0)
  // jsdom 里 `scrollLeft` 是普通属性、不会派发 `scroll` 事件，这里手动同步一次，
  // 保证"受控状态"在测试与真实环境里表现一致
  if (el.scrollLeft !== scrollLeft.value) scrollLeft.value = el.scrollLeft ?? 0
}

function measure() {
  const el = scroller.value
  if (!el) return
  viewportWidth.value = el.clientWidth ?? 0
  // 量完顺手让滚动条也重算一次（它的轨道宽同样是 DOM 决定的）
  scrollbarRef.value?.measure?.()
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
defineExpose({
  /**
   * 测试钩子：重新量宽度并把受控状态同步到滚动容器的真值。
   *
   * 存在的理由：jsdom 不做布局，尺寸只能由测试"按下去"；而生产环境里"尺寸变了"
   * 是由 `ResizeObserver` 通知的，测试里不保证触发。与其让每个用例去猜
   * observer 有没有跑，不如给一个明确的重测入口 —— 真实浏览器里不依赖它。
   */
  refresh() {
    measure()
    onScroll()
    draw()
  },
})

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
//
// `restoreScrollIfNeeded()` 必须在重画**之前**跑：密度重算会把滚动容器打回 0，
// 先把偏移放回去，这一帧画出来才是"用户点的那一格仍在眼前"。
watch(
  () => [
    props.density,
    props.selection,
    props.focusTs,
    props.height,
    viewportWidth.value,
    contentWidth.value,
    scrollLeft.value,
  ],
  () => {
    restoreScrollIfNeeded()
    draw()
  },
  { flush: 'post' },
)
</script>

<template>
  <div class="density" :data-focus="focusSnapshot">
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
          title="改用「区间曲线」（**默认视图**：一条曲线看趋势，拖播放头 / 窗口带选时段）"
          @click="emit('update:view', 'curve')"
        >
          改用曲线
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
          :style="canvasStyle"
          :title="hoverTitle"
        ></canvas>
        <div v-if="!bars.length" class="empty-hint">暂无密度数据</div>
      </div>
    </div>
    <!-- 内容比视口宽时才需要滚动条；它的位置由滚动容器回流（`scrollTo`） -->
    <TimelineScrollbar
      v-if="scrollable"
      ref="scrollbarRef"
      :content-width="contentWidth"
      :viewport-width="viewportWidth"
      :scroll-left="scrollLeft"
      :ticks="ticks"
      @scroll="scrollTo"
    />
  </div>
</template>

<style scoped>
.density {
  border-top: 1px solid var(--border);
  background: var(--panel);
  padding: 6px 14px 8px;
  /* 时间轴整体不允许把左列撑宽：宽度由父级给，内部要滚就自己滚 */
  min-width: 0;
  overflow: hidden;
}
.mini.switch { color: var(--muted); }

.track-scroll {
  /* `width: 100%` 是**必须写**的，不能只靠 `overflow-x: auto`：
     作为 grid/flex 项目的滚动容器，`min-width` 默认 `auto` ⇒ 宽度会按
     内容（画布的固有宽度）算，于是把它上面的 `.body` 顶宽、右栏被挤出去
     （实测：视口 762px 时这里宽 1860px）。详见 style.css 里 `.app` 的说明。 */
  width: 100%;
  min-width: 0;
  overflow-x: auto;
  overflow-y: hidden;
  cursor: pointer;
}
.track-scroll.scrollable { padding-bottom: 4px; }
/* 自绘滚动条接管之后，**隐藏原生横向滚动条** —— 两条并排会互相矛盾 */
.track-scroll::-webkit-scrollbar { height: 0; }
.track-scroll { scrollbar-width: none; }
.track { position: relative; }
/* 可滚动时把画布**吸附**在可视区左侧：它只有一屏宽，不 sticky 就会被滚出视野。
   ⚠️ 宽度**由内联样式按像素给**（见 `canvasStyle` 的注释），CSS 这边只能留一个
   "还没量到宽度"时的兜底 —— 写成 `width: 100%` 会让画布和它的 sticky 包含块
   （`.track`）一样宽，sticky 就完全没有挪动余地，直接失效。 */
.track-canvas { display: block; position: sticky; left: 0; width: 100%; }
.empty-hint { position: absolute; inset: 0; display: flex; align-items: center; color: var(--muted); font-size: 12px; }
</style>
