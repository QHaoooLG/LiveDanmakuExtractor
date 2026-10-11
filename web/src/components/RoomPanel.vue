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
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { formatDuration } from '../format.js'
import {
  MAX_SUGGESTIONS,
  buildSuggestions,
  filterSuggestions,
  normalizeRoomEntry,
} from '../rooms.js'

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
  /**
   * `/api/health` 的 `attention`（后端 `assess_health()` 的结果）。
   *
   * 需求（"健康横幅更显眼"）：光有一句"有房间登录失效"还不够 ——
   * 用户得知道**是哪个房间**。这里用它给出问题的房间行标一个醒目的角标，
   * 与顶栏的常驻角标、上方的横幅形成"三处指向同一件事"。
   */
  attention: { type: Object, default: null },
})

const emit = defineEmits(['add', 'select', 'remove', 'reconnect'])

const draft = ref('')
const linkDraft = ref('')
/** 房间号输入框：空状态引导横幅的「去添加房间」会把光标送到这里 */
const draftInput = ref(null)
/** 直播间链接输入框（见 `focusInput`：优先把光标放这里） */
const linkInput = ref(null)

/**
 * 把光标移到房间号输入框（供 `App.vue` 的空状态引导调用）。
 *
 * 优先给**链接**输入框：需求方给的用法就是"粘一条直播间链接"，粘链接比让人去
 * 地址栏里手工挑数字省事。链接框本来也认纯数字房间号，所以这一选不限制用户。
 *
 * 不需要 `await nextTick()`：这两个输入框都**不是** `v-if` 出来的，DOM 一直在，
 * 事件处理函数里直接 `focus()` 就行。
 */
function focusInput() {
  const target = linkInput.value ?? draftInput.value
  if (!target) return
  target.focus()
  // 已有内容时全选：用户想换房间号时直接覆盖，不用先 Ctrl+A
  target.select()
  // 需求 #8：点「去添加房间」时顺手把下拉列表也展开（候选就在眼前）
  openList()
}

/**
 * 点下拉列表**外面**就收起它。
 *
 * 用 `mousedown`（而不是 `click`）并检查 `relatedTarget` 不可靠 —— 直接判断
 * 点击目标是否落在输入框容器内最稳：`mousedown` 早于 `blur`，
 * 所以点候选项时不会被提前收起（候选项自己的 `mousedown.prevent` 也保了一手）。
 */
function onDocumentMouseDown(event) {
  if (!open.value) return
  if (draftBox.value?.contains(event.target)) return
  closeList()
}

onMounted(() => document.addEventListener('mousedown', onDocumentMouseDown))
onBeforeUnmount(() => document.removeEventListener('mousedown', onDocumentMouseDown))

defineExpose({ focusInput })

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

/**
 * 下拉列表的候选：本地记忆 ∪ 已监控房间，去重后按房间号倒序。
 *
 * 逻辑在 `rooms.js`（纯函数、可离线单测）：昵称优先级、"曾采集 vs 监控中"、
 * 以及**兼容旧的纯字符串记忆格式**都在那边。
 */
const suggestions = computed(() =>
  buildSuggestions({ history: props.history ?? [], rooms: props.rooms ?? [] }),
)

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

// --------------------------------------------------------------------------- //
// 房间号下拉列表（需求 #8）
// --------------------------------------------------------------------------- //

/** 输入框所在的容器：点外面要收起下拉，所以监听它的失焦 */
const draftBox = ref(null)
const open = ref(false)
/** 键盘高亮项（-1 = 没有高亮；回车时若为 -1 就走"直接提交输入框里的值"） */
const highlight = ref(-1)
/**
 * 抑制下一次 `focus` 展开下拉。
 *
 * 为什么需要：`choose()` 选中之后要**收起**列表（用户已经选完了），
 * 但它还会把焦点交回输入框 —— 而输入框的 `@focus` 会立刻把列表又打开。
 * 结果就是"选了等于没选，列表永远关不掉"（实测踩到）。
 */
let suppressOpen = false

/**
 * 下拉列表内容：**按输入过滤**，空输入时按"最近的排前面"给全部候选。
 *
 * 过滤同时匹配房间号与昵称 —— 用户可能只记得"那个叫某某的主播"，
 * 只按房间号过滤会让他找不到（昵称本来就是这次要加的信息，不用白不用）。
 * 实现在 `rooms.js` 的 `filterSuggestions()`（纯函数，可离线单测）。
 */
const filtered = computed(() =>
  filterSuggestions(suggestions.value, draft.value, MAX_SUGGESTIONS),
)

/** 选中候选项：填进输入框（**不直接添加** —— 填错号就添加是很糟的体验）。 */
function choose(item) {
  draft.value = String(item.room_id)
  closeList()
  // 把焦点交回输入框方便继续输入，但**不要**因此又把列表打开（见 `suppressOpen`）
  suppressOpen = true
  draftInput.value?.focus()
  suppressOpen = false
}

/**
 * 候选项的悬停说明。
 *
 * 「据库推测」这一档必须**说清楚它不是权威值** —— 需求方实测发现过
 * "房间号和昵称不匹配"，原因是采集库推导的是"留言最多的人"，
 * 热门房间里那往往是**观众**（实测 4/4 全错），不能当成主播名。
 */
function itemTitle(item) {
  if (item.monitored) return `正在监控：${item.anchor_name || `房间 ${item.room_id}`}`
  if (item.name_source === 'guess') {
    return `房间 ${item.room_id}　曾采集过（这个昵称是"该房间发言最多的人"，可能不是主播）`
  }
  if (item.anchor_name) return `房间 ${item.room_id}　曾采集过：${item.anchor_name} 的直播间`
  return `填入房间号 ${item.room_id}`
}

function openList() {
  if (suppressOpen) return
  open.value = true
  highlight.value = filtered.value.length ? 0 : -1
}

function closeList() {
  open.value = false
  highlight.value = -1
}

/** 上下键在候选里移动（到两端就停住，不绕圈 —— 绕圈很容易选错）。 */
function move(step) {
  if (!open.value) {
    openList()
    return
  }
  const total = filtered.value.length
  if (!total) return
  const next = highlight.value + step
  highlight.value = Math.min(Math.max(next, 0), total - 1)
}

/**
 * 回车：**有高亮就先填进去，再让用户确认**。
 *
 * 为什么不直接提交高亮项：用户可能只是想看看列表里有什么，
 * 回车直接开始监控一个他没细看的房间号是很重的副作用。
 * 填进输入框后他再按一次回车就真的添加了（`submit()` 由「添加」按钮和
 * 输入框的回车共同驱动）—— 也就是说**:第一次回车选中、第二次回车添加**。
 */
function onEnter() {
  const item = filtered.value[highlight.value]
  if (open.value && item) {
    choose(item)
    return
  }
  submit()
}

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
  closeList()
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
  const issue = roomIssue(room)
  return `只看 ${name}${room.error ? `（${room.error}）` : ''}${issue ? `　⚠ ${issue}` : ''}`
}

/** 该房间是否是健康提示里点名的房间之一（需求：知道**是哪个**房间出了问题）。 */
function roomIssue(room) {
  const payload = props.attention
  if (!payload || payload.level === 'ok') return ''
  const flagged = (payload.rooms ?? []).map(String)
  if (!flagged.includes(String(room?.room_id))) return ''
  return payload.title || '需要处理'
}
</script>

<template>
  <section class="room-panel">
    <!-- 直播间链接输入（需求 5）：放在房间号输入框**上面**。
         粘一条 `https://live.bilibili.com/8432038?...` 就能拿到房间号，
         省掉"从地址栏里手工挑数字"这一步。 -->
    <div class="link-row">
      <input
        ref="linkInput"
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

    <!-- 房间号输入 + 添加（需求点 2 / 需求 #8）：
         输入框自带**下拉列表**，每个房间号旁边显示主播昵称（曾采集过的也能显示）。 -->
    <div class="add-row">
      <div ref="draftBox" class="combo">
        <input
          ref="draftInput"
          v-model="draft"
          type="text"
          inputmode="numeric"
          autocomplete="off"
          placeholder="输入房间号，如 804456"
          :disabled="busy"
          role="combobox"
          aria-autocomplete="list"
          :aria-expanded="open"
          aria-controls="room-suggest"
          @focus="openList"
          @input="openList"
          @keydown.down.prevent="move(1)"
          @keydown.up.prevent="move(-1)"
          @keydown.esc="closeList"
          @keyup.enter="onEnter"
        />
        <!-- 下拉列表（需求 #8）。刻意**不用**原生 `<datalist>`：
             它只能显示 `value`，没法在房间号旁边带主播昵称；而且各家浏览器
             对它的样式与键盘行为都不一致。 -->
        <div v-if="open && filtered.length" id="room-suggest" class="combo-list" role="listbox">
          <button
            v-for="(item, index) in filtered"
            :key="item.room_id"
            type="button"
            role="option"
            :aria-selected="index === highlight"
            class="combo-item"
            :class="{ active: index === highlight, current: Number(item.room_id) === current }"
            :title="itemTitle(item)"
            @mouseenter="highlight = index"
            @mousedown.prevent="choose(item)"
          >
            <span class="cid">{{ item.room_id }}</span>
            <span v-if="item.anchor_name" class="cname" :class="{ guess: item.name_source === 'guess' }">
              {{ item.anchor_name }}
            </span>
            <span v-if="item.monitored" class="cbadge">监控中</span>
            <span v-else-if="item.name_source === 'guess'" class="cbadge muted">据库推测</span>
            <span v-else-if="item.anchor_name" class="cbadge muted">曾采集</span>
          </button>
        </div>
      </div>
      <button
        :disabled="!draft.trim() || busy || (Number(draft.trim()) === current && currentMonitored)"
        :title="Number(draft.trim()) === current && currentMonitored ? '该房间已在监控中' : '开始监控这个房间'"
        @click="submit"
      >
        {{ busy ? '添加中…' : '添加' }}
      </button>
    </div>
    <!-- 这里原来还有一排「记忆：804456 · 某某」的胶囊。
         需求方要求**去掉**：房间号输入框已经有下拉列表（同样的信息、且能搜昵称），
         两处并列反而让人不知道该点哪个。同理删掉了原来的 `<datalist>`。
         下拉列表是唯一的记忆入口，`history` / `rooms` 两个来源在那里合并。 -->

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
      <!-- 健康提示点名的房间：加一个常驻角标（需求：一眼看出是哪个房间） -->
      <span v-if="roomIssue(room)" class="issue" :class="attention?.level" :title="roomIssue(room)">
        ⚠
      </span>
      <!-- 新增：这个房间停在了"等手动重连"，就地给按钮 ——
           用户看到的是"某个房间出问题了"，让他还要去上面横幅里找对应操作
           是多余的一步（横幅是汇总视角，房间行是"就是它"的视角）。 -->
      <button
        v-if="room.manual_reconnect_needed"
        class="reconnect"
        :title="`自动重连已尝试 ${room.manual_reconnect_limit ?? 5} 次仍未成功；点这里让它立刻再连一次`"
        @click.stop="emit('reconnect', room.room_id)"
      >
        重连
      </button>
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

.add-row { display: flex; gap: 6px; align-items: flex-start; }
.add-row input { flex: 1; min-width: 0; }

/* 房间号下拉列表（需求 #8）：容器要 `position: relative`，列表绝对定位浮在上层 */
.combo { position: relative; flex: 1; min-width: 0; }
.combo input { width: 100%; }
.combo-list {
  position: absolute; z-index: 30; left: 0; right: 0; top: calc(100% + 2px);
  max-height: 220px; overflow-y: auto;
  background: var(--panel-2); border: 1px solid var(--border); border-radius: 6px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5);
  padding: 3px;
}
.combo-item {
  display: flex; align-items: baseline; gap: 6px; width: 100%;
  padding: 3px 7px; border: none; border-radius: 4px;
  background: none; color: var(--text); text-align: left; cursor: pointer;
  font-size: 12px;
}
.combo-item.active { background: #1d2b36; }
.combo-item.current .cid { color: var(--accent); }
.combo-item .cid { font-variant-numeric: tabular-nums; flex-shrink: 0; }
.combo-item .cname {
  flex: 1; min-width: 0; color: #9ecbf5;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
/* 「据库推测」的名字用灰斜体：它不是权威主播名，不能看起来像 */
.combo-item .cname.guess { color: var(--muted); font-style: italic; }
.combo-item .cbadge {
  flex-shrink: 0; font-size: 10px; color: var(--accent);
  border: 1px solid #1e4d63; border-radius: 999px; padding: 0 6px;
}
.combo-item .cbadge.muted { color: var(--muted); border-color: var(--border); }

/* 原来「记忆」胶囊的 .history/.hint/.chip 样式已随那排胶囊一起删掉：
   需求方要求把记忆房间号只留在下拉列表里（两处并列会让人不知道该点哪个）。 */

h3 .scope {
  margin-left: 6px; font-size: 11px; font-weight: 400;
  color: #8b93a1; border: 1px solid #2c313a; border-radius: 999px; padding: 1px 7px;
}
.room-item { align-items: flex-start; }
/* 「重连」按钮：与「×」并排，但用强调色区分（一个是修复动作，一个是危险动作） */
.room-item .reconnect {
  padding: 1px 7px; font-size: 12px;
  color: var(--accent); border-color: #1e4d63;
}
.room-item .rid { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.room-item .rname { font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.room-item .rname.muted { color: var(--muted); }
.room-item .rsub { font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums; }
/* 出问题的房间：常驻 ⚠ 角标（红=需要处理，黄=注意） */
.issue { flex-shrink: 0; font-size: 13px; line-height: 1; cursor: help; }
.issue.warn { color: #e3b341; }
.issue.alert { color: #f85149; }
</style>
