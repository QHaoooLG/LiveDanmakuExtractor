<script setup>
/**
 * 时间轴下方的**横向滚动条**（需求 #7：超过 60 格时启用左右滑动）。
 *
 * ## 为什么不直接用原生滚动条
 *
 * 原生滚动条在深色主题 + canvas 时间轴下面有两个问题：
 *   1. 高度只有 8px，抓取很别扭，而且**没法显示"我在整段的哪个位置"**
 *      （时间轴是"整场直播的一根尺"，位置本身就是信息）；
 *   2. 各浏览器/DPI 下宽度与样式不一致，无法与"格数/时间范围"对齐。
 *
 * 所以这里自己画：轨道 + 可拖动的滑块 + 两端的**时间刻度**。
 *
 * ## 交互（三条都要能用，缺一条都会让人骂）
 *
 *   * **拖滑块**：按下后跟手，鼠标移出窗口也不丢（`setPointerCapture`）；
 *   * **点轨道空白**：跳到那个位置（相当于滚动一屏）；
 *   * **方向键 / Home / End**：键盘可达（滑块是 `role="scrollbar"` 的可聚焦元素）。
 *
 * 状态是**受控**的：本组件不持有滚动位置，只把目标位置 emit 出去，
 * 由持有滚动容器的父组件去 `scrollLeft = ...`，再把新的 `scrollLeft` 传回来。
 * 这样"滚动条"与"内容"永远只有一个真相（滚动容器的实际位置），不会各说各话。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { scrollbarMetrics } from '../timeline.js'

const props = defineProps({
  /** 内容总宽（px） */
  contentWidth: { type: Number, default: 0 },
  /** 视口宽（px） */
  viewportWidth: { type: Number, default: 0 },
  /** 当前横向偏移（px） */
  scrollLeft: { type: Number, default: 0 },
  /** 刻度文字：`{ ratio: 0..1, text: string }[]`，缺省不画 */
  ticks: { type: Array, default: () => [] },
})

const emit = defineEmits(['scroll'])

/** 轨道元素的宽度（用来把"内容比例"换算成像素） */
const track = ref(null)
const trackPx = ref(0)
const dragging = ref(false)

function measure() {
  trackPx.value = track.value?.clientWidth ?? 0
}

const metrics = computed(() =>
  scrollbarMetrics({
    contentWidth: props.contentWidth,
    viewportWidth: props.viewportWidth,
    trackPx: trackPx.value,
  }),
)

/** 滑块的左偏移（px）：把"内容偏移比例"映射到"滑块可移动距离"。 */
const thumbOffset = computed(() => {
  const { maxScroll, maxThumbOffset } = metrics.value
  if (maxScroll <= 0) return 0
  const ratio = Math.min(Math.max(props.scrollLeft / maxScroll, 0), 1)
  return ratio * maxThumbOffset
})

/** 把"滑块偏移（px）"反算回"内容偏移（px）"。 */
function scrollFromThumbOffset(offset) {
  const { maxScroll, maxThumbOffset } = metrics.value
  if (maxThumbOffset <= 0) return 0
  const ratio = Math.min(Math.max(offset / maxThumbOffset, 0), 1)
  return ratio * maxScroll
}

// -- 拖拽 -------------------------------------------------------------- //

let dragStartX = 0
let dragStartThumb = 0

function onThumbDown(event) {
  // 阻止冒泡：否则会先被轨道的"点空白跳转"接管，滑块会瞬移
  event.stopPropagation()
  event.preventDefault()
  dragging.value = true
  dragStartX = event.clientX
  dragStartThumb = thumbOffset.value
  // 指针捕获：鼠标拖出组件甚至拖出窗口也继续跟手
  event.target.setPointerCapture?.(event.pointerId)
}

function onPointerMove(event) {
  if (!dragging.value) return
  const next = dragStartThumb + (event.clientX - dragStartX)
  emit('scroll', scrollFromThumbOffset(next))
}

function onPointerUp(event) {
  if (!dragging.value) return
  dragging.value = false
  event.target.releasePointerCapture?.(event.pointerId)
}

// -- 点轨道 / 键盘 ------------------------------------------------------ //

function onTrackDown(event) {
  measure()
  const rect = track.value?.getBoundingClientRect()
  if (!rect) return
  // 点在滑块上由 `onThumbDown` 处理（它 stopPropagation 了），这里只处理空白处：
  // 目标是"让点击处尽量靠近视口中央"，所以减去半个滑块宽
  const wanted = event.clientX - rect.left - metrics.value.thumbPx / 2
  emit('scroll', scrollFromThumbOffset(wanted))
}

const STEP_PX = 120

function onKeydown(event) {
  const { maxScroll } = metrics.value
  if (maxScroll <= 0) return
  const page = Math.max(props.viewportWidth * 0.8, STEP_PX)
  const moves = {
    ArrowLeft: props.scrollLeft - STEP_PX,
    ArrowRight: props.scrollLeft + STEP_PX,
    PageUp: props.scrollLeft - page,
    PageDown: props.scrollLeft + page,
    Home: 0,
    End: maxScroll,
  }
  if (!(event.key in moves)) return
  event.preventDefault()
  emit('scroll', Math.min(Math.max(moves[event.key], 0), maxScroll))
}

defineExpose({ measure })

let observer = null

onMounted(() => {
  // 轨道宽**只能从 DOM 读**（它是 `width: 100%` 的弹性元素），而布局在挂载时
  // 还没算完 —— 所以挂载后量一次，并交给 `ResizeObserver` 持续跟。
  measure()
  if (typeof ResizeObserver !== 'undefined' && track.value) {
    observer = new ResizeObserver(measure)
    observer.observe(track.value)
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
  <div class="tl-scrollbar">
    <div
      ref="track"
      class="tl-track"
      role="scrollbar"
      tabindex="0"
      aria-orientation="horizontal"
      :aria-valuemin="0"
      :aria-valuemax="Math.round(contentWidth - viewportWidth)"
      :aria-valuenow="Math.round(scrollLeft)"
      title="拖动滑块或点这里可以左右移动时间轴（也可以用方向键 / Home / End）"
      @pointerdown="onTrackDown"
      @pointermove="onPointerMove"
      @pointerup="onPointerUp"
      @pointercancel="onPointerUp"
      @keydown="onKeydown"
    >
      <div
        class="tl-thumb"
        :class="{ dragging }"
        :style="{ width: `${metrics.thumbPx}px`, transform: `translateX(${thumbOffset}px)` }"
        @pointerdown="onThumbDown"
      ></div>
    </div>
    <!-- 两端时间刻度：时间轴是"整场直播的一根尺"，让用户知道自己在整段的哪一头 -->
    <div class="tl-scale">
      <span v-for="(tick, index) in ticks" :key="index" class="tl-scale-item">{{ tick.text }}</span>
    </div>
  </div>
</template>

<style scoped>
.tl-scrollbar {
  margin-top: 4px;
  user-select: none;
  /* 自绘滚动条也不许把左列撑宽（它是"按内容定宽"那条链的最外一环） */
  min-width: 0;
  overflow: hidden;
}
.tl-track {
  position: relative;
  height: 10px;
  border-radius: 5px;
  background: #171a20;
  cursor: pointer;
  outline: none;
}
/* 键盘聚焦时给个可见的提示（不然 tabindex 等于没用） */
.tl-track:focus-visible { box-shadow: 0 0 0 2px #1e4d63; }
.tl-thumb {
  position: absolute;
  top: 0;
  left: 0;
  height: 100%;
  border-radius: 5px;
  background: #3f6d8e;
  cursor: grab;
  transition: background 0.12s ease;
}
.tl-thumb:hover { background: #4f86ad; }
.tl-thumb.dragging { background: var(--accent); cursor: grabbing; transition: none; }
.tl-scale {
  display: flex;
  justify-content: space-between;
  margin-top: 2px;
  font-size: 10px;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}
.tl-scale-item { white-space: nowrap; }
</style>
