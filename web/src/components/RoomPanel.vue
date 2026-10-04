<script setup>
/**
 * 右侧信息栏顶部：**直播间信息 + 房间号输入/添加 + 已监控房间列表**。
 *
 * 这是本轮的排版改动核心：房间号输入从顶栏搬到右侧栏顶部（需求点 2），
 * 因为"房间"本来就是右侧栏的主体内容 —— 输入框紧挨着它作用的列表，
 * 顶栏则腾出来只放**跨面板的过滤/操作**。
 *
 * 另外两个细节值得保留：
 *   * 输入框用**原生 `<datalist>`** 给出历史房间号建议：浏览器自带
 *     "输入几个字符就过滤建议"的行为，比自绘下拉省事且键盘可达；
 *   * 主播昵称高亮显示（`.anchor`），关掉面板的其它信息也一眼能看到是谁的直播间。
 */
import { computed, ref } from 'vue'
import { formatDuration } from '../format.js'

const props = defineProps({
  /** 当前选中房间号（null = 全部房间） */
  current: { type: [Number, null], default: null },
  /** `/api/room-info` 的结果：当前房间的直播间信息 */
  info: { type: Object, default: null },
  /** 已监控房间运行时快照数组 */
  rooms: { type: Array, default: () => [] },
  /** 本地记忆过的房间号（输入建议） */
  history: { type: Array, default: () => [] },
  /** 添加请求进行中 */
  busy: { type: Boolean, default: false },
})

const emit = defineEmits(['add', 'select', 'remove'])

const draft = ref('')
const linkDraft = ref('')

const title = computed(() => {
  if (props.info?.title) return props.info.title
  if (props.current) return `房间 ${props.current}`
  return '未选择房间'
})

const anchorName = computed(() => props.info?.anchor_name ?? '')

const liveLabel = computed(() => {
  if (props.info == null) return ''
  return props.info.live_status === 1 ? '直播中' : '未开播'
})

/** 当前房间是否已在监控中（决定「添加」按钮该不该可点）。 */
const currentMonitored = computed(() => {
  if (props.current == null) return false
  return (props.rooms ?? []).some((room) => room.room_id === props.current)
})

/** 建议列表 = 本地记忆 ∪ 已监控房间，去重后按数字倒序（最近的排前面）。 */
const suggestions = computed(() => {
  const values = new Set()
  for (const item of props.history) if (item) values.add(String(item))
  for (const room of props.rooms) if (room?.room_id) values.add(String(room.room_id))
  return [...values].sort((a, b) => Number(b) - Number(a) || a.localeCompare(b))
})

/**
 * 从直播间链接里取出房间号（**仅用于即时反馈**）。
 *
 * 权威实现是后端的 `rooms.parse_room_input()`（也更严格：它只认 B 站域名、
 * 且只把路径第一段当房间号）。这里做一份轻量的是为了用户体验 ——
 * 粘完链接立刻能看到"已识别房间号 xxx"，而不是等一次网络往返。
 * 真正的解析仍由后端负责（`/api/room-info` 与 `POST /api/rooms` 都会重新解析），
 * 所以两边不一致时以后端为准，不会出现"前端猜错、后端照做"。
 */
function extractRoomId(text) {
  const value = (text ?? '').trim()
  if (!value) return null
  if (/^\d+$/.test(value)) return Number(value)
  const byPath = value.match(/live\.bilibili\.com\/(\d{1,12})(?:[/?#]|$)/i)
  if (byPath) return Number(byPath[1])
  const byParam = value.match(/[?&]room_?id=(\d{1,12})/i)
  if (byParam) return Number(byParam[1])
  return null
}

const linkRoomId = computed(() => extractRoomId(linkDraft.value))
const linkInvalid = computed(
  () => Boolean(linkDraft.value.trim()) && linkRoomId.value == null,
)

/** 识别出房间号 → 填进主输入框（用户可见、可改，不直接添加）。 */
function applyLink() {
  if (linkRoomId.value != null) draft.value = String(linkRoomId.value)
}

/** 回车 = 识别并**直接添加**（少一次点击，符合"粘完就走"的习惯）。 */
function submitLink() {
  const value = linkDraft.value.trim()
  if (!value || props.busy) return
  if (linkRoomId.value == null) return
  emit('add', String(linkRoomId.value))
  linkDraft.value = ''
}

function submit() {
  const value = draft.value.trim()
  if (!value || props.busy) return
  emit('add', value)
  draft.value = ''
}

function stateLabel(room) {
  const state = room.conn_state ?? ''
  if (state === 'STREAMING') return '接收中'
  if (state === 'RECONNECTING') return '重连中'
  if (state === 'CONNECTING' || state === 'AUTH_SENDING') return '连接中'
  if (state.startsWith('FAILED')) return '失败'
  if (state === 'PAUSED') return '已暂停'
  return state || '-'
}

function stateClass(room) {
  const state = room.conn_state ?? ''
  if (state === 'STREAMING') return 'on'
  if (state.startsWith('FAILED')) return 'off'
  return 'wait'
}

function roomTitle(room) {
  const name = room.anchor_name ? `${room.anchor_name}的直播间` : `房间 ${room.room_id}`
  return `只看 ${name}${room.error ? `（${room.error}）` : ''}`
}
</script>

<template>
  <section class="room-panel">
    <!-- 直播间链接输入（需求 5）：放在房间号输入框**上面**。
         粘一条 `https://live.bilibili.com/8432038?...` 就能拿到房间号，
         省掉"从地址栏里手工挑数字"这一步。 -->
    <div class="link-row">
      <input
        v-model="linkDraft"
        type="text"
        placeholder="粘贴直播间链接，如 https://live.bilibili.com/8432038"
        :disabled="busy"
        :class="{ invalid: linkInvalid }"
        @input="applyLink"
        @keyup.enter="submitLink"
      />
      <button
        :disabled="linkRoomId == null || busy"
        :title="linkRoomId != null ? `识别为房间号 ${linkRoomId}，直接添加` : '还没有识别出房间号'"
        @click="submitLink"
      >
        解析并添加
      </button>
    </div>
    <div class="link-hint">
      <template v-if="linkInvalid">
        <span class="bad">未能从该链接识别出房间号（只支持 B 站直播链接）</span>
      </template>
      <template v-else-if="linkRoomId != null">
        <span class="ok">已识别房间号 {{ linkRoomId }}，已填入下方输入框；回车可直接添加</span>
      </template>
      <template v-else>
        <span>支持 `live.bilibili.com/<房间号>`，带查询串也可以</span>
      </template>
    </div>

    <!-- 直播间信息（需求点 3）：顶栏被搬到右侧栏后，这里是"当前在看谁"的唯一入口 -->
    <div class="live-head" :class="{ none: !current }">
      <div class="live-title">
        <template v-if="current">
          <span v-if="anchorName" class="anchor" :title="`uid ${info?.anchor_uid ?? '-'}`">
            {{ anchorName }}
          </span>
          <span class="suffix">的直播间</span>
        </template>
        <span v-else class="idle">未选择房间</span>
      </div>
      <div class="live-meta">
        <span v-if="current" class="tag">房间号 {{ current }}</span>
        <span v-if="info?.short_id" class="tag">短号 {{ info.short_id }}</span>
        <span v-if="liveLabel" class="tag" :class="info?.live_status === 1 ? 'living' : ''">
          {{ liveLabel }}
        </span>
        <span v-if="current && info && info.monitored === false" class="tag warn">未监控</span>
        <span v-if="currentMonitored" class="tag" title="该房间已在监控中，无需重复添加">已监控</span>
      </div>
    </div>

    <!-- 房间号输入 + 添加（需求点 2）：datalist 给出本地记忆过的房间号 -->
    <div class="add-row">
      <input
        v-model="draft"
        list="room-history"
        type="text"
        inputmode="numeric"
        placeholder="输入房间号，如 804456"
        :disabled="busy"
        @keyup.enter="submit"
      />
      <button
        :disabled="!draft.trim() || busy || (Number(draft.trim()) === current && currentMonitored)"
        :title="Number(draft.trim()) === current && currentMonitored ? '该房间已在监控中' : '开始监控这个房间'"
        @click="submit"
      >
        {{ busy ? '添加中…' : '添加' }}
      </button>
      <datalist id="room-history">
        <option v-for="value in suggestions" :key="value" :value="value"></option>
      </datalist>
    </div>
    <div v-if="suggestions.length" class="history">
      <span class="hint">记忆：</span>
      <button
        v-for="value in suggestions.slice(0, 6)"
        :key="value"
        class="chip"
        :class="{ active: Number(value) === current }"
        :title="`填入 ${value}`"
        @click="draft = value"
      >
        {{ value }}
      </button>
    </div>

    <h3>
      监控中
      <span class="scope">{{ rooms.length }} 个</span>
    </h3>
    <div
      v-for="room in rooms"
      :key="room.room_id"
      class="room-item"
      :class="{ active: room.room_id === current }"
      :title="roomTitle(room)"
      @click="emit('select', room.room_id)"
    >
      <span class="dot" :class="stateClass(room)"></span>
      <span class="rid">
        <span v-if="room.anchor_name" class="rname">{{ room.anchor_name }}</span>
        <span v-else class="rname muted">房间 {{ room.room_id }}</span>
        <span class="rsub">{{ room.room_id }} · {{ stateLabel(room) }} · {{ room.stored }} 条</span>
      </span>
      <button style="padding:1px 7px;font-size:12px" title="停止监控该房间" @click.stop="emit('remove', room.room_id)">
        ×
      </button>
    </div>
    <div v-if="!rooms.length" class="empty" style="padding:10px 0;text-align:left">
      尚未监控任何房间<br /><small>在上方输入房间号后点「添加」</small>
    </div>
  </section>
</template>

<style scoped>
.room-panel { display: flex; flex-direction: column; }

/* 直播间链接输入（在房间号输入框上面） */
.link-row { display: flex; gap: 6px; margin-bottom: 4px; }
.link-row input { flex: 1; min-width: 0; font-size: 12px; }
.link-row input.invalid { border-color: #7f1d1d; }
.link-hint { font-size: 11px; color: var(--muted); margin-bottom: 8px; line-height: 1.5; }
.link-hint .ok { color: #86efac; }
.link-hint .bad { color: #fca5a5; }

/* 直播间信息块：主播昵称高亮，是右侧栏最上面、最醒目的东西 */
.live-head {
  border: 1px solid #24435a; background: linear-gradient(180deg, #172936, #15181d);
  border-radius: 8px; padding: 9px 11px; margin-bottom: 9px;
}
.live-head.none { border-color: var(--border); background: var(--panel-2); }
.live-title { font-size: 15px; line-height: 1.35; word-break: break-all; }
.anchor {
  color: #fbbf24; font-weight: 700;
  text-shadow: 0 0 10px rgba(251, 191, 36, 0.35);
}
.suffix { color: var(--text); }
.idle { color: var(--muted); font-size: 14px; }
.live-meta { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 7px; }
.tag.living { color: #fca5a5; border-color: #7f1d1d; }
.tag.warn { color: #fcd34d; border-color: #78550f; }

.add-row { display: flex; gap: 6px; }
.add-row input { flex: 1; min-width: 0; }
.history { display: flex; flex-wrap: wrap; align-items: center; gap: 5px; margin-top: 6px; }
.hint { color: var(--muted); font-size: 11px; }
.chip {
  padding: 1px 8px; font-size: 12px; border-radius: 999px;
  color: var(--muted); background: var(--panel-2);
  font-variant-numeric: tabular-nums;
}
.chip.active { color: var(--accent); border-color: #1e4d63; }

h3 .scope {
  margin-left: 6px; font-size: 11px; font-weight: 400;
  color: #8b93a1; border: 1px solid #2c313a; border-radius: 999px; padding: 1px 7px;
}
.room-item { align-items: flex-start; }
.room-item .rid { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.room-item .rname { font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.room-item .rname.muted { color: var(--muted); }
.room-item .rsub { font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums; }
</style>
