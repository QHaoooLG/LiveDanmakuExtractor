<script setup>
/**
 * 时间轴的**表头**（两个渲染方式共用）。
 *
 * 抽出来的理由：canvas 版与 DOM 版（回退用）必须长得**一模一样**。
 * 两份拷贝的代价不是"多写一遍"，而是以后只修了一份 —— 那样用户切换渲染方式
 * 就会看到不同的概要行、不同的按钮状态，而且很难发现（因为两个都要手点才看得到）。
 * 所以凡是"文字与按钮"的部分只留这一处，两个渲染器只负责**画柱子**。
 *
 * 唯一的例外是右上角那个「改成 canvas/DOM 绘制」的开关：它只在一个渲染器
 * 内部有意义（切回去就不该再显示"换成 canvas"），所以由各渲染器自己放在
 * `DensityHead` 之后，而不是塞进来当槽位。
 */
import { BUCKET_CHOICES, formatStamp } from '../timeline.js'

defineProps({
  /** `/api/density` 的响应（用来判断哪个格宽按钮是选中态） */
  density: { type: Object, default: null },
  /** 概要行文本（`timeline.buildSummary()` 的结果） */
  summary: { type: String, default: '' },
  /** 正在定位的时间点 */
  focusTs: { type: Number, default: null },
  /** 桶数（可滚动时才显示） */
  count: { type: Number, default: 0 },
  /** 是否横向可滚动（决定要不要显示滚动提示） */
  scrollable: { type: Boolean, default: false },
})

const emit = defineEmits(['update:bucket'])
</script>

<template>
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
      @click="emit('update:bucket', choice.value)"
    >
      {{ choice.label }}
    </button>
    <span v-if="focusTs" class="focus">定位 {{ formatStamp(focusTs) }}</span>
    <span v-if="scrollable" class="scroll-hint" title="鼠标滚轮即可左右滑动时间轴">
      {{ count }} 格 · 可横向滚动
    </span>
    <!-- 渲染方式开关由调用方插在这里（同一个渲染器里"换回去"才有意义） -->
    <slot name="switch"></slot>
  </div>
</template>

<style scoped>
.density-head { display: flex; align-items: center; gap: 6px; font-size: 11px; margin-bottom: 4px; flex-wrap: wrap; }
.density-head .title { color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; font-weight: 600; }
.density-head .summary { color: var(--muted); }
.density-head .spacer { flex: 1; }
.density-head .bucket-label { color: var(--muted); margin-left: 4px; }
.density-head .focus { color: var(--accent); border: 1px solid #1e4d63; border-radius: 999px; padding: 0 8px; margin-left: 4px; }
.density-head .scroll-hint { color: var(--accent); border: 1px solid #1e4d63; border-radius: 999px; padding: 0 8px; margin-left: 4px; }
.mini { padding: 1px 8px; font-size: 11px; }
.mini.on { color: var(--bg); background: var(--accent); border-color: var(--accent); font-weight: 600; }
</style>
