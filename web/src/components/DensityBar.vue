<script setup>
/**
 * 时间轴的**门面**：按偏好把绘制交给 canvas 版或 DOM 版。
 *
 * ## 为什么要有这一层
 *
 * 优化项 ⑩ 要求"改成 canvas，**保留旧版以便回滚**"。回滚不能只是"代码还在 git 里"
 * —— 那样真出问题时用户只能等我们发版。所以这里做成一个**当场可切**的开关：
 *
 *   * 默认 `canvas`（新实现）；
 *   * 表头右侧一个「改用 DOM / 改用 canvas」按钮，切换写进 `localStorage`
 *     （键 `bili-danmaku.timeline-render`），刷新后仍然生效；
 *   * 万一 canvas 在某个环境下画不出来（或画错），用户自己一步就能退回旧版，
 *     不用清缓存、不用等更新。
 *
 * ## 为什么不动态 import
 *
 * 两个实现都只有几 KB，`v-if` 静态引入就够了；动态 import 会引入
 * `<Suspense>` 和首屏空白，得不偿失。这里真正要控制的是**渲染成本**，
 * 而 `v-if` 已经保证了没被选中的那个不会被渲染（也不会 mount 画布/监听器）。
 *
 * 偏好值**在这个组件里就地管**（不提到 `App.vue`）：它是纯粹的显示细节，
 * 提上去只会让父组件多一份与它无关的状态。
 */
import { ref, watch } from 'vue'
import DensityCanvas from './DensityCanvas.vue'
import DensityDom from './DensityDom.vue'

const RENDER_KEY = 'bili-danmaku.timeline-render'
const RENDER_CHOICES = ['canvas', 'dom']

const props = defineProps({
  /** `/api/density` 的响应 */
  density: { type: Object, default: null },
  /** 当前历史窗口 `{ lo, hi }` */
  range: { type: Object, default: null },
  /** 正在定位的时间点 */
  focusTs: { type: Number, default: null },
  /** 密度条高度（px） */
  height: { type: Number, default: 46 },
})

const emit = defineEmits(['jump', 'update:bucket'])

/**
 * 读出偏好。
 *
 * 读不到 / 值不认识时一律回 **canvas**：`localStorage` 抛异常的情形
 * （隐私模式、被策略禁用）不该让时间轴消失。
 */
function loadRenderer() {
  try {
    const saved = window.localStorage?.getItem(RENDER_KEY)
    if (RENDER_CHOICES.includes(saved)) return saved
  } catch {
    /* 读不到就用默认值 */
  }
  return 'canvas'
}

const renderer = ref(loadRenderer())

watch(renderer, (value) => {
  try {
    window.localStorage?.setItem(RENDER_KEY, value)
  } catch {
    /* 记不住也能用，只是下次刷新回到默认值 */
  }
})
</script>

<template>
  <DensityCanvas
    v-if="renderer === 'canvas'"
    :density="density"
    :range="range"
    :focus-ts="focusTs"
    :height="height"
    :renderer="renderer"
    @jump="emit('jump', $event)"
    @update:bucket="emit('update:bucket', $event)"
    @update:renderer="renderer = $event"
  />
  <DensityDom
    v-else
    :density="density"
    :range="range"
    :focus-ts="focusTs"
    :height="height"
    :renderer="renderer"
    @jump="emit('jump', $event)"
    @update:bucket="emit('update:bucket', $event)"
    @update:renderer="renderer = $event"
  />
</template>
