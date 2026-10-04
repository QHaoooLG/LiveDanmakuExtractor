<script setup>
/**
 * 时间轴密度条 —— **DOM 版**（原来的实现，**保留以便回滚**）。
 *
 * ## 为什么还留着它
 *
 * 优化项 ⑩ 的要求是"改成 canvas 绘制，**保留旧版以便回滚**"。这里就是那个旧版：
 * 每个桶渲染一个真 `<button>`，因此**天然键盘可达**（Tab 能走、回车能跳），
 * 也不需要处理 HiDPI / 可视区裁剪。代价是桶多时节点数量大（整库按 1 分钟切
 * 会有几千个节点，每次统计刷新都要 diff 一遍）。
 *
 * 两份实现的关系（改之前先读）：
 *   * **表头**共用 `DensityHead.vue` —— 两份拷贝以后只会修一份，
 *     用户切换渲染方式就会看到不同的概要行；
 *   * **数值**全部来自 `src/timeline.js`（高度比例 / 焦点下标 / 概要行 /
 *     每格宽度 / 命中测试 / 滚轮位移），否则两边的数字会漂移；
 *   * 这个文件只负责**在 DOM 里摆柱子**。
 *
 * 高亮的语义（两版都必须一致）：
 *   * **有 `focusTs`（刚点了某一格）时只高亮那一格** —— 用户点哪格就该亮哪格；
 *   * 没有 `focusTs` 时高亮整个历史窗口覆盖的区间（"我正在看哪一段"）。
 */
import { computed, ref } from 'vue'
import {
  DENSE_BARS,
  MIN_SLOT_PX,
  SCROLL_BARS,
  buildBars,
  buildSummary,
  focusIndexOf,
  inWindow,
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
  renderer: { type: String, default: 'dom' },
})

const emit = defineEmits(['jump', 'update:bucket', 'update:renderer'])

const bars = computed(() => buildBars(props.density))
const span = computed(() => spanOf(props.density))
const summary = computed(() => buildSummary(props.density))
const dense = computed(() => bars.value.length > DENSE_BARS)
const scrollable = computed(() => bars.value.length > SCROLL_BARS)
const focusIndex = computed(() => focusIndexOf(bars.value, props.focusTs, span.value))

/** 可滚动时的时间轴最小总宽（每格 `MIN_SLOT_PX`，含间隙）。 */
const trackMinWidth = computed(() =>
  scrollable.value
    ? `${trackWidth({ count: bars.value.length, viewportWidth: 0, scrollable: true })}px`
    : null,
)

const scroller = ref(null)

/**
 * 滚轮 → **横向滚动**。
 *
 * 位移量由 `timeline.wheelDelta()` 判定（返回 0 = 不该拦截，让页面正常滚）——
 * 与 canvas 版共用同一份判断，免得两边"滚到尽头该不该放行"的行为不一样。
 */
function onWheel(event) {
  const el = scroller.value
  if (!el || !scrollable.value) return
  const delta = wheelDelta({
    deltaY: event.deltaY,
    deltaX: event.deltaX,
    scrollLeft: el.scrollLeft,
    maxScroll: el.scrollWidth - el.clientWidth,
  })
  if (!delta) return
  event.preventDefault()
  el.scrollLeft += delta
}

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
          title="改用 canvas 绘制（几千个桶时更省资源；DOM 版仍可用作回退）"
          @click="emit('update:renderer', 'canvas')"
        >
          改用 canvas
        </button>
      </template>
    </DensityHead>
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
          :class="{ active: inWindow(bar, { focusTs, range, spanS: span }), focus: index === focusIndex }"
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
.mini.switch { color: var(--muted); }

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
