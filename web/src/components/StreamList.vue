<script setup>
/**
 * 弹幕流（实时 + 历史合并后的视图）。
 *
 * 三条关键行为：
 *   1. 自动滚动只在用户**贴底**时生效；上滚查看历史时暂停并提示新消息数，
 *      否则读历史会被不断涌入的新消息顶走。
 *   2. **向上翻页时保持视口**：历史是"往数组头部插"的，如果不补偿 `scrollTop`，
 *      用户会被瞬间弹走（读到一半的内容跑掉了）。补偿算法见 `loadOlderFromTop()`。
 *   3. 关键词高亮用分段渲染而不是 v-html —— 弹幕正文是不可信用户输入，
 *      直接注入 HTML 会造成 XSS。
 *
 * 数据形状：合并视图里的元素统一带 `id`（SQLite 主键）。历史条目来自
 * `/api/history` 的瘦投影（`emots` 在顶层），实时条目是完整 `Event`
 * （表情在 `extra.emots`）—— 两边都要能渲染，见 `bodySegments()`。
 *
 * **发言者昵称默认原样显示**；`anonymizeName=true` 时只留首字符、其余用 `*` 掩盖
 * （开关在上下文条 `FilterBar.vue` 的"浏览历史"那一栏最右侧）。
 * 状态由 `App.vue` 持有并写 localStorage —— 与"每格时长"那种组件内部偏好不同，
 * 这个开关的**位置**在别的组件里，所以状态必须上提，否则两边会不同步。
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { createFlickerGuard } from '../loadingIndicator.js'
import {
  displayName,
  eventText,
  formatTime,
  isMasked,
  maskName,
  medalText,
  textSegments,
} from '../format.js'
import { findTimestampIndex } from '../timeline.js'

const props = defineProps({
  events: { type: Array, required: true },
  keyword: { type: String, default: '' },
  /** 正在加载更旧的一页 */
  loadingOlder: { type: Boolean, default: false },
  /** 还有更旧的数据（false 时不再触发 near-top） */
  hasOlder: { type: Boolean, default: false },
  /** 是否匿名化发言者昵称（只留首字符；默认**不**匿名） */
  anonymizeName: { type: Boolean, default: false },
  /**
   * 已打标记的时间点（`[recv_ts, ...]`）。
   *
   * 用**时间戳**而不是 `id` 匹配：历史窗口与实时层的 `id` 语义一致，
   * 但标记可能钉在已被清理/尚未加载的条目上，时间戳才是一直都在的锚。
   */
  markedStamps: { type: Array, default: () => [] },
})

const emit = defineEmits(['near-top', 'near-bottom', 'mark'])

const MAX_RENDERED = 400
/** 距顶部多少 px 触发"加载更旧" */
const NEAR_TOP_PX = 240
/** 标记匹配的容差（秒）：标记取的是"那一条的时间戳"，理论上精确相等；
 *  留一点容差是为了吸收 JSON 浮点往返的尾差。 */
const MARK_TOLERANCE = 0.0005

const container = ref(null)
const stick = ref(true)
const pending = ref(0)
/** 是否已经滚到顶部边缘（避免反复触发） */
const atTopEdge = ref(false)

const markedSet = computed(() => props.markedStamps.map((value) => Number(value)))

/**
 * 毫秒精度的标记集合。
 *
 * `markedStamps` 最多 500 个，而 `isMarked()` 在模板里**每行调用 3 次**
 * （class / title / 星号）—— 400 行就是 400×3×500 = **60 万次比较/事件**。
 * 把时间戳**四舍五入到毫秒**（`MARK_TOLERANCE` 是 0.5 毫秒）之后，
 * 匹配退化成一次 `Set.has()`，既快又与原容差语义等价：两个时间戳相差
 * ≤0.5 ms 时，它们四舍五入到毫秒必然落在同一格或相邻格 —— 相邻格时
 * 差值是 1 ms，超出容差，所以为了**完全等价**这里额外查前后各一格。
 */
const markedMsSet = computed(() => {
  const set = new Set()
  for (const value of markedSet.value) {
    if (Number.isFinite(value)) set.add(Math.round(value * 1000))
  }
  return set
})

function isMarked(event) {
  const ts = Number(event?.recv_ts)
  if (!Number.isFinite(ts)) return false
  const key = Math.round(ts * 1000)
  const set = markedMsSet.value
  return set.has(key) || set.has(key - 1) || set.has(key + 1)
}

const rendered = computed(() => {
  const all = props.events
  return all.length > MAX_RENDERED ? all.slice(all.length - MAX_RENDERED) : all
})

/**
 * 渲染行：把模板里**每个事件都要算一遍**的东西预先算好。
 *
 * 原先模板直接调 `isMarked(event)`（×3）与 `bodySegments(event)`，而它们是
 * 普通函数 ⇒ **每次重渲染对全部 400 行重算**，而重渲染**每来一条弹幕都会发生**。
 * `bodySegments` 还会 `{...event}` 复制对象 + 建一个新 `extra`，即每行每事件
 * 2 个新对象。预先算一次之后，模板只读字段。
 */
const viewRows = computed(() => {
  const rows = []
  for (const event of rendered.value) {
    rows.push({
      key: rowKey(event),
      event,
      type: event.type,
      marked: isMarked(event),
      body: bodySegments(event),
    })
  }
  return rows
})

function segments(text) {
  const value = text ?? ''
  const keyword = props.keyword?.trim()
  if (!keyword) return [{ text: value, hit: false }]

  const haystack = value.toLowerCase()
  const needle = keyword.toLowerCase()
  const out = []
  let cursor = 0

  while (cursor < value.length) {
    const at = haystack.indexOf(needle, cursor)
    if (at === -1) {
      out.push({ text: value.slice(cursor), hit: false })
      break
    }
    if (at > cursor) out.push({ text: value.slice(cursor, at), hit: false })
    out.push({ text: value.slice(at, at + keyword.length), hit: true })
    cursor = at + keyword.length
  }
  return out.length ? out : [{ text: value, hit: false }]
}

function scrollToBottom() {
  const el = container.value
  if (el) el.scrollTop = el.scrollHeight
}

/**
 * 正文分段（文本 / 表情）。
 *
 * 历史条目把表情放在顶层 `emots`（瘦投影），实时条目在 `extra.emots`（完整 Event）。
 * `format.textSegments()` 读的是后者，所以这里给历史条目补一个 `extra` 视图，
 * 避免为了兼容而在两个地方各写一套分段逻辑。
 */
function bodySegments(event) {
  if (event.emots && !event.extra?.emots) {
    return textSegments({ ...event, extra: { ...(event.extra ?? {}), emots: event.emots } })
  }
  return textSegments(event)
}

function rowKey(event) {
  return event.id ?? `${event.session_id}-${event.seq}`
}

function onScroll() {
  const el = container.value
  if (!el) return
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40
  stick.value = atBottom
  if (atBottom) {
    pending.value = 0
    emit('near-bottom')
  }

  // 顶部边缘：只在"从非边缘变成边缘"时触发一次，避免连续滚动刷爆请求
  const nearTop = el.scrollTop <= NEAR_TOP_PX
  if (nearTop && !atTopEdge.value && props.hasOlder) emit('near-top')
  atTopEdge.value = nearTop
}

/**
 * 「正在加载更早的弹幕…」的**防闪烁**门控（需求 2026-10-09 第七轮）。
 *
 * 向上翻一页通常只要 25~40 ms：提示立刻出现又立刻消失，用户看到的是"闪一下"，
 * 比不显示更像卡顿。所以用 `createFlickerGuard` 管两件事：
 * **延迟 150 ms 才显示**（快请求不打扰）、**显示后至少停 300 ms**（不闪断）。
 */
const showLoading = ref(false)
const flickerGuard = createFlickerGuard({
  onChange: (visible) => {
    showLoading.value = visible
  },
})
watch(() => props.loadingOlder, (loading) => flickerGuard.update(loading), { immediate: true })
onBeforeUnmount(() => flickerGuard.dispose())

watch(
  () => props.events.length,
  async (next, previous) => {
    if (stick.value) {
      await nextTick()
      scrollToBottom()
      pending.value = 0
    } else {
      pending.value += Math.max(0, next - (previous ?? 0))
    }
  },
)

/**
 * 往头部插入了 N 条之后，把 `scrollTop` 补偿同样的高度差。
 *
 * 必须在 DOM 更新**之后**读 `scrollHeight`（`await nextTick()`），
 * 并且用"新高度 - 旧高度"的差值补齐；只记 `scrollTop` 是不够的，
 * 因为浏览器在内容变高时给出的 `scrollTop` 语义已经变了。
 */
async function preserveAnchor(previousHeight) {
  await nextTick()
  const el = container.value
  if (!el) return
  el.scrollTop += el.scrollHeight - previousHeight
  atTopEdge.value = el.scrollTop <= NEAR_TOP_PX
}

/** 由外部在"开始加载更旧一页"时调用，返回当前高度供之后补偿。 */
function captureHeight() {
  return container.value?.scrollHeight ?? 0
}

async function jumpToBottom() {
  stick.value = true
  pending.value = 0
  await nextTick()
  scrollToBottom()
}

/**
 * 滚到某个时刻那一条（时间轴跳转用）。
 *
 * ## 为什么不能再用"滚到底"糊过去
 *
 * 点某一格时 `jumpTo` 取的是"该格结束时刻之前的 300 条"，窗口右端就在那一格附近，
 * 所以"滚到底"曾经是对的。但合并视图里还可能混着更新的内容，那时滚到底会落在
 * **当下** —— 用户看到的就是"点了没反应"（需求方实测反馈）。
 *
 * 所以这里按**时间戳定位**：找到目标时刻那一条，把它滚进视口。命中不到就退化成
 * "最近的一条"，再不行才滚到底 —— 保证永远有反馈。
 *
 * 返回是否真的定位到了某一条（调用方据此可以提示"这一格没有弹幕"）。
 */
async function jumpToTimestamp(ts, spanS = 0) {
  await nextTick()
  const el = container.value
  if (!el) return false
  const rows = el.querySelectorAll('[data-ts]')
  if (!rows.length) return false

  const stamps = [...rows].map((row) => Number(row.dataset.ts))
  const index = findTimestampIndex(stamps, Number(ts), spanS, { mode: 'nearest' })
  if (index < 0) return false

  const row = rows[index]
  // 放在视口上 1/3 处而不是贴顶：用户点的是"这一段时间"，
  // 上方留一点能同时看到它**之前**的上下文
  el.scrollTop = Math.max(0, row.offsetTop - el.clientHeight / 3)
  // 手动滚过之后就不该再被"贴底自动滚动"带走（否则来一条新弹幕就被顶走）
  stick.value = el.scrollHeight - el.scrollTop - el.clientHeight < 40
  atTopEdge.value = el.scrollTop <= NEAR_TOP_PX
  return true
}

defineExpose({
  jumpToBottom,
  scrollToBottom: jumpToBottom,
  jumpToTimestamp,
  captureHeight,
  preserveAnchor,
})
</script>

<template>
  <div class="stream-wrap">
    <!-- 「正在加载更早的弹幕…」：2px 不确定进度条 + 文案（需求 2026-10-09 第七轮）。
         可见性由 `loadingIndicator.js` 的防闪烁门控决定（快请求不打扰、显示后至少停 300ms）。
         骨架行曾作为"占位"方案试过一版，按需求方 2026-10-09 的决定**去掉** ——
         插入高度的补偿本来就由 `preserveAnchor()` 负责，不需要靠占位撑。 -->
    <div v-if="showLoading" class="loading-more" role="status" aria-live="polite">
      <div class="loading-bar" aria-hidden="true"><span class="loading-bar-fill"></span></div>
      <div class="loading-text">正在加载更早的弹幕…</div>
    </div>
    <div class="stream" ref="container" @scroll.passive="onScroll">
      <div v-if="!rendered.length" class="empty">
        暂无消息<br />
        <small>若房间未开播或消息稀疏，可能需要等待；也可在右侧添加房间。</small>
      </div>

      <div
        v-for="row in viewRows"
        :key="row.key"
        class="stream-line"
        :class="[`type-${row.type}`, { marked: row.marked }]"
        :data-ts="row.event.recv_ts"
      >
        <span class="time">{{ formatTime(row.event.recv_ts) }}</span>

        <!-- 钉一条弹幕 = 打一个"钉在具体某条"的标记（时间点仍来自 recv_ts） -->
        <button
          class="mark-toggle"
          :class="{ on: row.marked }"
          :title="row.marked ? '这条已打标记（点此再打一个）' : '在这条弹幕上打标记'"
          @click="emit('mark', row.event)"
        >
          {{ row.marked ? '★' : '☆' }}
        </button>

        <template v-if="row.type !== 'room_stats' && row.type !== 'notice'">
          <span class="medal" v-if="medalText(row.event)">{{ medalText(row.event) }}</span>
          <!-- 昵称：默认原样；打开「匿名昵称」后只留首字符。
               平台自己的打码（未登录时的 `宅***`）无法本地还原，那类行用灰色斜体标出来。 -->
          <span
            class="name"
            :class="{ masked: isMasked(row.event), anonymized: anonymizeName }"
            :title="anonymizeName ? '已匿名（可在上下文条关闭）' : displayName(row.event)"
          >
            {{ anonymizeName ? maskName(displayName(row.event)) : displayName(row.event) }}
          </span>
        </template>

        <span class="text">
          <!-- 表情段渲染成图片（链接来自 extra.emots / 历史条目的 emots），文本段走关键词高亮 -->
          <template v-for="(segment, index) in row.body" :key="index">
            <img
              v-if="segment.type === 'emot'"
              class="emot"
              :src="segment.url"
              :alt="segment.text"
              :title="segment.text"
              loading="lazy"
              referrerpolicy="no-referrer"
            />
            <template v-else>
              <template v-for="(piece, inner) in segments(segment.text)" :key="inner">
                <mark v-if="piece.hit">{{ piece.text }}</mark>
                <template v-else>{{ piece.text }}</template>
              </template>
            </template>
          </template>
        </span>
      </div>

      <button v-if="pending > 0" class="jump" @click="jumpToBottom">
        {{ pending }} 条新消息 ↓
      </button>
    </div>
  </div>
</template>

<style scoped>
/* 用 flex 让弹幕流占据剩余高度，时间轴固定在下方 */
.stream-wrap { display: flex; flex-direction: column; min-height: 0; min-width: 0; }
.stream { flex: 1; min-height: 0; }
/* 「正在加载更早的弹幕…」——比原来显眼得多：进度条抓余光 + 加重文案 */
.loading-more {
  position: relative;
  padding: 0 14px 8px;
  font-size: 12px;
  color: var(--accent);
  background: #16232c;
  border-bottom: 1px solid var(--border);
}
/* 顶部 2px **不确定**进度条：不知道进度，所以来回滑动（比假百分比诚实） */
.loading-bar {
  position: absolute; top: 0; left: 0; right: 0; height: 2px;
  overflow: hidden; background: #0f1115;
}
.loading-bar-fill {
  display: block; width: 35%; height: 100%;
  background: linear-gradient(90deg, transparent, var(--accent), transparent);
  animation: loading-slide 1.1s ease-in-out infinite;
}
@keyframes loading-slide {
  0% { transform: translateX(-100%); }
  100% { transform: translateX(340%); }
}
.loading-text { padding: 7px 0 6px; font-weight: 600; letter-spacing: .02em; }
/* 有"减少动态效果"偏好的用户：关掉动画，但**保留提示本身**（信息不能被砍掉） */
@media (prefers-reduced-motion: reduce) {
  .loading-bar-fill { animation: none; width: 100%; opacity: .5; }
}
/* 标记按钮：默认很淡（400 行都有它，太抢眼会压过弹幕正文），悬停/已标记时才亮 */
.mark-toggle {
  background: none; border: none; padding: 0 2px; cursor: pointer;
  color: #3d4450; font-size: 12px; line-height: 1.4; flex-shrink: 0;
}
.mark-toggle:hover { color: #fbbf24; }
.mark-toggle.on { color: #fbbf24; }
.stream-line.marked { background: #221d10; }
/* 匿名化：用等宽 + 略暗表示"这不是真名"，但**不**改字号（保持行高稳定） */
.stream-line .name.anonymized { color: #7c8493; font-variant-numeric: tabular-nums; }
</style>
