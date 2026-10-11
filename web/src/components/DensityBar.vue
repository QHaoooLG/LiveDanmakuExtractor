<script setup>
/**
 * 时间轴的**视图门面**：按偏好把绘制交给哪个实现。
 *
 * ## 默认已换成 `curve`（Phase 3 转正 = 方案 A）
 *
 * | 值 | 组件 | 状态 |
 * |----|------|------|
 * | `curve`（**默认**） | `TimelineCurve.vue` | **方案 A 转正**（需求 2026-10-09：直接换成 Phase 3 的区间曲线，准备压测） |
 * | `canvas` | `DensityCanvas.vue` | 保留一个版本周期做**回滚**（点表头右侧「改用柱状」当场退回） |
 *
 * 需求方 2026-10-09 先要求"不上线 phase 3，等实测"，随后拍板
 * **"将时间轴直接换为方案 A 也就是 phase 3 的样式，准备进行压力测试"** ——
 * 所以默认值从 `canvas` 改成 `curve`。
 *
 * ## 为什么 `VIEW_KEY` 也跟着换了名字（不是只改默认值）
 *
 * 只改默认值的话，"以前点过「改用柱状」的人"会**继续被 localStorage 里那个
 * `canvas` 钉住** —— 界面上看着像"换了默认却对我没生效"。把键换成 `-v2`，
 * 旧值一律作废，所有人都从新默认值起步；之后用户在表头做的选择照旧记在新键上
 * （所以想回柱状的人点一次仍然记得住）。**这是"直接换掉"，不是"新装的人换掉"。**
 *
 * ## 为什么默认值写在"读不到就用 curve"这一条上
 *
 * `localStorage` 可能抛异常（隐私模式、被策略禁用），也可能存着不认识的值
 * （老版本留下的 `dom`、或 `-v2` 之前的手改值）。任何异常/不认识的值都回落到
 * `DEFAULT_VIEW` —— 时间轴**不能因为一个显示偏好就消失**。
 *
 * ## 为什么不做动态 import
 *
 * 两个实现都只有几 KB，`v-if` 静态引入就够了；动态 import 会引入 `<Suspense>`
 * 和首屏空白，得不偿失。这里真正要控制的是**渲染成本**，而 `v-if` 已经保证了
 * 没被选中的那个不会被渲染（也不会 mount 画布/监听器）。
 *
 * ## 「每格 X 分钟」在两个视图下是**同一件事**（2026-10-09 第四轮改口径）
 *
 * 曾经把它拆成两个事件（柱状 → `update:bucket` 重新拉密度；曲线 → `update:span`
 * 只改窗口段长）。那个设计是错的：**"每格"就是密度采样粒度**，
 * 曲线视图也需要它 —— 不重新拉密度的话，曲线上的点还是旧粒度，
 * 表头却写着新粒度（"换了档位但曲线没变"的假象）。
 * 现在两种视图都发 `update:bucket`，由 `useHistory` 重新拉密度并重绘。
 */
import { ref, watch } from 'vue'
import DensityCanvas from './DensityCanvas.vue'
import TimelineCurve from './TimelineCurve.vue'

/** localStorage 键。**值域变了要小心**：老键里的值（含 `dom`）一律作废。 */
const VIEW_KEY = 'bili-danmaku.timeline-view-v2'
const VIEW_CHOICES = ['curve', 'canvas']
/** 默认视图 = 方案 A / Phase 3 的区间曲线（需求方 2026-10-09 拍板转正）。 */
const DEFAULT_VIEW = 'curve'

defineProps({
  /** `/api/density` 的响应 */
  density: { type: Object, default: null },
  /** 当前历史窗口 `{ lo, hi, spanS, anchorTs }`（"我在看哪一段"的唯一真相） */
  selection: { type: Object, default: null },
  /** **列表已加载**的时间范围（只读信息层，曲线视图会画一条淡色带） */
  loadedRange: { type: Object, default: null },
  /** 正在定位的时间点（= 窗口锚点） */
  focusTs: { type: Number, default: null },
  /** 当前窗口的段长（秒）—— 曲线视图用它画窗口带 */
  spanS: { type: Number, default: null },
  /** 密度条高度（px） */
  height: { type: Number, default: 46 },
})

const emit = defineEmits(['jump', 'update:bucket'])

/**
 * 读出偏好。
 *
 * 读不到 / 值不认识时一律回 `DEFAULT_VIEW`（`curve`）：
 * 时间轴不能因为一个显示偏好就消失。
 */
function loadView() {
  try {
    const saved = window.localStorage?.getItem(VIEW_KEY)
    if (VIEW_CHOICES.includes(saved)) return saved
  } catch {
    /* 读不到就用默认值 */
  }
  return DEFAULT_VIEW
}

const view = ref(loadView())

watch(view, (value) => {
  try {
    window.localStorage?.setItem(VIEW_KEY, value)
  } catch {
    /* 记不住也能用，只是下次刷新回到默认值 */
  }
})

/** 换粒度：两种视图都是"重新拉密度"，所以只发一个事件。 */
function onBucket(value) {
  emit('update:bucket', Number(value))
}
</script>

<template>
  <DensityCanvas
    v-if="view === 'canvas'"
    :density="density"
    :selection="selection"
    :focus-ts="focusTs"
    :height="height"
    :view="view"
    @jump="emit('jump', $event)"
    @update:bucket="onBucket"
    @update:view="view = $event"
  />
  <TimelineCurve
    v-else
    :density="density"
    :selection="selection"
    :loaded-range="loadedRange"
    :focus-ts="focusTs"
    :span-s="spanS"
    :height="height"
    :view="view"
    @jump="emit('jump', $event)"
    @update:bucket="onBucket"
    @update:view="view = $event"
  />
</template>
