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
import { computed, nextTick, ref, watch } from 'vue'
import {
  displayName,
  eventText,
  formatTime,
  isMasked,
  maskName,
  medalText,
  textSegments,
} from '../format.js'

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

function isMarked(event) {
  const ts = Number(event?.recv_ts)
  if (!Number.isFinite(ts)) return false
  return markedSet.value.some((value) => Math.abs(value - ts) <= MARK_TOLERANCE)
}

const rendered = computed(() => {
  const all = props.events
  return all.length > MAX_RENDERED ? all.slice(all.length - MAX_RENDERED) : all
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
 * 跳到某个时间点后调用：把视口放到底部。
 *
 * 所点的那一格就在历史窗口的**末尾附近**（`jumpTo` 用"该格结束时刻"当窗口右端），
 * 所以滚到底 = 看到所点时刻的弹幕。语义上等同 `jumpToBottom()`，
 * 但保留独立名字是因为调用方的意图是"定位到某一格"，不是"追最新"。
 */
defineExpose({ jumpToBottom, scrollToBottom: jumpToBottom, captureHeight, preserveAnchor })
</script>

<template>
  <div class="stream-wrap">
    <div v-if="loadingOlder" class="loading-more">正在加载更早的弹幕…</div>
    <div class="stream" ref="container" @scroll.passive="onScroll">
      <div v-if="!rendered.length" class="empty">
        暂无消息<br />
        <small>若房间未开播或消息稀疏，可能需要等待；也可在右侧添加房间。</small>
      </div>

      <div
        v-for="event in rendered"
        :key="rowKey(event)"
        class="stream-line"
        :class="[`type-${event.type}`, { marked: isMarked(event) }]"
      >
        <span class="time">{{ formatTime(event.recv_ts) }}</span>

        <!-- 钉一条弹幕 = 打一个"钉在具体某条"的标记（时间点仍来自 recv_ts） -->
        <button
          class="mark-toggle"
          :class="{ on: isMarked(event) }"
          :title="isMarked(event) ? '这条已打标记（点此再打一个）' : '在这条弹幕上打标记'"
          @click="emit('mark', event)"
        >
          {{ isMarked(event) ? '★' : '☆' }}
        </button>

        <template v-if="event.type !== 'room_stats' && event.type !== 'notice'">
          <span class="medal" v-if="medalText(event)">{{ medalText(event) }}</span>
          <!-- 昵称：默认原样；打开「匿名昵称」后只留首字符。
               平台自己的打码（未登录时的 `宅***`）无法本地还原，那类行用灰色斜体标出来。 -->
          <span
            class="name"
            :class="{ masked: isMasked(event), anonymized: anonymizeName }"
            :title="anonymizeName ? '已匿名（可在上下文条关闭）' : displayName(event)"
          >
            {{ anonymizeName ? maskName(displayName(event)) : displayName(event) }}
          </span>
        </template>

        <span class="text">
          <!-- 表情段渲染成图片（链接来自 extra.emots / 历史条目的 emots），文本段走关键词高亮 -->
          <template v-for="(segment, index) in bodySegments(event)" :key="index">
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
.loading-more {
  padding: 4px 14px;
  font-size: 12px;
  color: var(--accent);
  background: #16232c;
  border-bottom: 1px solid var(--border);
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
