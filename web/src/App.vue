<script setup>
import { computed, onMounted, onUnmounted, ref, shallowRef, watch } from 'vue'
import { api } from './api.js'
import { axisStartTs as anchorStart } from './axisAnchor.js'
import { base64ToBytes } from './base64.js'
import { normalizeRoomHistory, NAME_SOURCE } from './rooms.js'
import { useHistory } from './composables/useHistory.js'
import { useLiveStream } from './composables/useLiveStream.js'
import { unify } from './eventShape.js'
import { TYPE_ORDER, typeLabel } from './format.js'
import { formatBucketSpan } from './timeline.js'
import {
  buildExportFilename,
  loadAnonymizeName,
  nextExportSeq,
  saveAnonymizeName,
} from './preferences.js'
import AnalysisPanel from './components/AnalysisPanel.vue'
import BookmarkBar from './components/BookmarkBar.vue'
import DensityBar from './components/DensityBar.vue'
import EmptyRoomsBanner from './components/EmptyRoomsBanner.vue'
import FilterBar from './components/FilterBar.vue'
import HealthBanner from './components/HealthBanner.vue'
import RoomPanel from './components/RoomPanel.vue'
import ScBanner from './components/ScBanner.vue'
import StatsPanel from './components/StatsPanel.vue'
import StreamList from './components/StreamList.vue'
import TopBar from './components/TopBar.vue'

const STATS_INTERVAL_MS = 3000
/** 分析刷新间隔：比统计低一档 —— 词频/刷屏是给人读的，不需要 3 秒一跳 */
const ANALYSIS_INTERVAL_MS = 6000
/**
 * 时间轴密度的刷新间隔。
 *
 * 与统计同频（3 秒太密、6 秒够用）：密度的聚合实测约 1ms/百桶，真正的成本是
 * 每次都要重算曲线的 SVG 路径，6 秒一跳在观感上是"平滑增长"。
 */
const DENSITY_REFRESH_MS = 6000
const TOAST_MS = 4000
const KEYWORD_MODE_KEY = 'bili-danmaku.keyword-mode'
const STATS_MODE_KEY = 'bili-danmaku.stats-mode'
const STATS_WINDOW_KEY = 'bili-danmaku.stats-window'
const ROOM_HISTORY_KEY = 'bili-danmaku.room-history'
const MAX_ROOM_HISTORY = 12
const DEFAULT_STATS_WINDOW = 300

const roomId = ref(null)
const enabledTypes = ref([...TYPE_ORDER])
const keyword = ref('')
const keywordMode = ref(localStorage.getItem(KEYWORD_MODE_KEY) || 'highlight')
const statsMode = ref(localStorage.getItem(STATS_MODE_KEY) || 'cumulative')
const statsWindow = ref(Number(localStorage.getItem(STATS_WINDOW_KEY)) || DEFAULT_STATS_WINDOW)
/**
 * 是否匿名化弹幕流的发言者昵称（只留首字符）。
 *
 * 状态放在这里（而不是 StreamList 内部）是因为**开关的位置在 FilterBar**
 * —— 两个组件各存一份必然会不同步。默认**不**匿名。
 */
const anonymizeName = ref(loadAnonymizeName(localStorage))
watch(anonymizeName, (value) => saveAnonymizeName(localStorage, value))
/** 正在导出统计数据（按钮置灰防连点） */
const exportingStats = ref(false)
/**
 * 统计 / 分析 / 会话列表 —— 都是**整体替换**的只读载荷，所以用 `shallowRef`。
 *
 * `ref` 会对整个 payload 做**深响应式代理**：`/api/stats` 每 3 秒、
 * `/api/analysis` 每 6 秒回来一次，每次都会新建一整套 Proxy 与依赖集合，
 * 而没有任何一处**原地修改**这些载荷（都是 `x.value = await …`）。
 * `shallowRef` 只跟踪 `.value` 的替换，语义完全一致、开销小得多。
 *
 * （同一个理由：`density` / `densitySource` 在 `useHistory` 里也是 `shallowRef`。）
 */
const stats = shallowRef(null)
const health = ref(null)
/**
 * 「把健康横幅叫回来」的计数（点顶栏常驻角标时自增）。
 *
 * 用计数而不是布尔值：横幅可以被关掉，角标点第二次也得管用 ——
 * 布尔值第二次值没变，`watch` 不会跑（见 `HealthBanner` 的 `revealKey`）。
 */
const attentionReveal = ref(0)
const toast = ref('')
const stopping = ref(false)
const adding = ref(false)
/** `/api/room-info` 的结果：右侧栏顶部的「XXX 的直播间」 */
const roomInfo = ref(null)
/** 本地记忆过的房间号（最近输入/添加过的排前面） */
const roomHistory = ref(loadRoomHistory())
/** 抓取会话列表（"只看某一次抓取"） */
const sessions = shallowRef([])
/** 本地感知分析（V3-b 3A）：词频 / 热点突增 / 疑似刷屏 / 疑似异常账号 */
const analysis = shallowRef(null)
/** 实时缓冲上限：进入历史浏览后收缩，避免合并视图无限增长 */
const liveLimit = ref(null)

const streamRef = ref(null)
/** 右侧「房间」面板：空状态引导要能把光标送到它的输入框 */
const roomPanelRef = ref(null)

/**
 * 本地记忆的房间号。
 *
 * 只存"用户主动输入/添加过"的房间号（不是服务端返回的房间列表）：
 * 目的是下次打开面板时不用重新敲 —— 服务端没在监控的房间号也要记得住，
 * 这正是"记忆"和"已监控列表"的区别。解析成真实房间号后**替换掉**
 * 原来的短号，避免记忆里同时留下短号和真实号。
 */
/**
 * 本地记忆的房间（最近输入/添加过的排前面）。
 *
 * 结构是 `{ room_id, anchor_name }`（**不再是纯字符串**）：需求 #8 要求下拉列表
 * 在房间号旁边显示主播昵称。`anchor_name` 可能为空 —— 那时界面只显示房间号。
 *
 * 兼容旧的纯字符串格式：老用户的 localStorage 里是 `["804456", ...]`，
 * 必须能读进来（读不了就等于把他们的记忆清空了），所以这里做一次就地迁移。
 */
function loadRoomHistory() {
  try {
    return normalizeRoomHistory(JSON.parse(localStorage.getItem(ROOM_HISTORY_KEY) || '[]'))
  } catch {
    return []
  }
}

function saveRoomHistory() {
  try {
    localStorage.setItem(ROOM_HISTORY_KEY, JSON.stringify(roomHistory.value))
  } catch {
    /* 存不下也不影响本次使用 */
  }
}

/** 记一个房间号（可带昵称）。解析成真实房间号后**替换掉**原来的短号。 */
function rememberRoom(value, resolved, anchorName = '') {
  const key = String(resolved ?? value).trim()
  if (!key) return
  const alias = String(value).trim()
  const previous = roomHistory.value.find((item) => item.room_id === key)
  // 添加房间时拿到的昵称来自 `/api/rooms` 的运行时缓存 ⇒ 是**权威**的
  const name = String(anchorName || previous?.anchor_name || '')
  const entry = {
    room_id: key,
    // 这次没拿到昵称就沿用上次记住的，别把已有信息抹掉
    anchor_name: name,
    name_source: anchorName
      ? NAME_SOURCE.live
      : previous?.name_source && previous.name_source !== NAME_SOURCE.guess
        ? previous.name_source
        : name
          ? NAME_SOURCE.guess
          : '',
  }
  roomHistory.value = [
    entry,
    ...roomHistory.value.filter((item) => item.room_id !== key && item.room_id !== alias),
  ].slice(0, MAX_ROOM_HISTORY)
  saveRoomHistory()
}

/**
 * 给记忆里的房间补昵称（需求 #8）。
 *
 * ## 两段式（这是"房间号和昵称不匹配"的修法）
 *
 * 采集库推导的"留言最多的人"**在热门房间里会选到观众**（实测 4/4 全错：
 * 8432038 推出 `Neuf-Sept`，真主播是 `血狼破军`）。所以分两步：
 *
 * 1. **先要库推导值**（`live=false`，不联网、秒回）：它至少能当"曾采集过"的
 *    提示，让下拉列表马上有东西；
 * 2. **再要权威昵称**（`live=true`，逐个房间查 B 站）：拿到就**覆盖**掉第 1 步的
 *    猜测值，并标成 `live`。界面只对**非 live** 的名字显示"据库推测"。
 *
 * 失败静默：这只是下拉列表上的锦上添花，不该弹错误打扰用户。
 */
async function fillRoomHistoryNames() {
  const ids = roomHistory.value.map((item) => item.room_id)
  if (!ids.length) return

  const apply = (names, guessed, source) => {
    let changed = false
    roomHistory.value = roomHistory.value.map((item) => {
      const name = names?.[item.room_id]
      if (!name) return item
      // 权威值永远覆盖；猜测值只补空缺，且不覆盖已有的权威值
      const isAuthoritative = source === 'live' || !(guessed ?? []).includes(item.room_id)
      if (!isAuthoritative && item.anchor_name) return item
      const nextSource = isAuthoritative ? NAME_SOURCE.live : NAME_SOURCE.guess
      if (item.anchor_name === String(name) && item.name_source === nextSource) return item
      changed = true
      return { ...item, anchor_name: String(name), name_source: nextSource }
    })
    if (changed) saveRoomHistory()
  }

  try {
    const quick = await api.roomNames(ids)
    apply(quick?.items, quick?.guessed, quick?.source)
  } catch {
    /* 静默 */
  }
  try {
    const live = await api.roomNames(ids, { live: true })
    apply(live?.items, live?.guessed, 'live')
  } catch {
    /* 静默：联网失败就保留"据库推测"的标注 */
  }
}

// 这几个偏好存本地：刷新页面后不用重新调
watch(keywordMode, (value) => localStorage.setItem(KEYWORD_MODE_KEY, value))
watch(statsMode, (value) => localStorage.setItem(STATS_MODE_KEY, value))
watch(statsWindow, (value) => localStorage.setItem(STATS_WINDOW_KEY, String(value)))

// 解构成顶层 ref，模板里由 Vue 自动解包（不要写成 live.events.value）
const {
  connected,
  rooms,
  events: liveEvents,
  errors,
  received,
  reconnect,
  setLimit,
} = useLiveStream(roomId)

/** 实时层上限：历史浏览时收缩到 300（`setLimit` 会顺手裁掉多余部分）。 */
watch(liveLimit, (value) => setLimit(value ?? undefined))

/**
 * 把实时事件补齐成与历史投影**同形**的条目。
 *
 * 实现在 `eventShape.js` 里（为了能单独校验与复用）。
 * ⚠️ 它内部**做了记忆化**，这是必需的：`liveEvents` 每来一条弹幕就换一个新数组，
 * 所以下面这个 `computed` 会每条事件重算一次；没有记忆化的话，
 * 每次重算都会把整个缓冲（最多 2000 条）浅拷贝一遍。
 * 详细论证见 `web/src/eventShape.js`。
 */
const allEvents = computed(() => liveEvents.value.map(unify))

const history = useHistory(roomId, allEvents, liveLimit, (message) => showToast(message))

/** 当前流里实际出现过的类型，用于把类型下拉收敛到有用集合。 */
const availableTypes = computed(() => {
  const seen = new Set()
  for (const event of allEvents.value) seen.add(event.type)
  return [...seen]
})

/** 类型下拉的选项：已知顺序优先，再补上流里出现过的未知类型。 */
const typeOptions = computed(() => {
  const seen = new Set([...TYPE_ORDER, ...availableTypes.value])
  return [...seen].map((type) => ({ value: type, label: typeLabel(type) }))
})

/** 类型多选面板里的条数：用当前缓冲统计，帮助判断哪类值得看。 */
const typeCounts = computed(() => {
  const counts = {}
  for (const event of allEvents.value) counts[event.type] = (counts[event.type] ?? 0) + 1
  return counts
})

/** 房间 + 类型过滤后的结果（关键词还没参与）。 */
const scoped = computed(() => {
  const types = enabledTypes.value
  const room = roomId.value
  return history.merged.value.filter(
    (event) => types.includes(event.type) && (room == null || event.room_id === room),
  )
})

/**
 * 最终展示用事件。
 *
 * 关键词有两种模式：
 *   * `highlight`：只高亮不过滤（默认）—— 避免"搜不到就以为没数据"的误判；
 *   * `filter`：只显示命中的弹幕；切回高亮即恢复全部。
 */
const visible = computed(() => {
  const needle = keyword.value.trim().toLowerCase()
  if (keywordMode.value !== 'filter' || !needle) return scoped.value
  return scoped.value.filter((event) => (event.text || '').toLowerCase().includes(needle))
})

const uptime = computed(() => health.value?.uptime_s ?? 0)
const statsStartedAt = computed(
  () => stats.value?.cumulative?.started_at ?? stats.value?.window_started_at ?? 0,
)

/**
 * 可标记的"此刻"= 当前可见的最后一条弹幕的时间。
 *
 * 刻意用**可见的最后一条**而不是墙钟：正在看历史时，墙钟会标到"现在"，
 * 而用户以为标的是屏幕上那一段 —— 那种错误无法从界面上察觉。
 */
const playheadTs = computed(() => {
  const list = visible.value
  return list.length ? Number(list[list.length - 1].recv_ts) : null
})

/** 标记的时间点集合（弹幕行上的 ★ 靠它判定）。 */
const markedStamps = computed(() => history.bookmarks.value.map((item) => item.recv_ts))

function showToast(message) {
  toast.value = message
  setTimeout(() => {
    if (toast.value === message) toast.value = ''
  }, TOAST_MS)
}

/**
 * 健康横幅上的「去登录」。
 *
 * 为什么不能直接在这里扫码：**扫码需要图形界面**（要显示二维码），
 * 而 Web 面板是浏览器里的页面 —— 让它去画二维码会引入一套完全不同的实现。
 * 托盘应用本来就有启动面板（带扫码登录），所以这里只负责把用户指过去。
 */
function showLoginHint() {
  showToast('请在托盘图标上右键 →「显示启动面板」，在面板顶部点「扫码登录」')
}

/**
 * 手动重连若干房间（需求 2026-10-09：自动重连 5 次后放弃，改由用户手动重连）。
 *
 * 三个刻意的设计：
 *
 * 1. **按房间号逐个发**，而不是一个"全部重连"的批量端点：后端只在
 *    `WAITING_MANUAL` 状态下受理，逐个发才能如实反馈"哪几个受理了"；
 *    也避免"一键重连"把一堆连接正常的房间也掐了。
 * 2. `accepted === false` **不是错误**（那是"当前不需要重连"，是好消息），
 *    所以分开统计、分开措辞 —— 把它显示成一次失败会让用户以为又坏了。
 * 3. 结果用 toast 说清楚"受理了几个／几个不用重连"，否则用户点完不知道
 *    按钮到底起没起作用（这是自动重连停下之后唯一的人工动作，必须有反馈）。
 */
async function reconnectRooms(rooms) {
  const list = (rooms ?? []).filter((item) => Number.isFinite(Number(item)))
  if (!list.length) return
  let accepted = 0
  let declined = 0
  const failed = []
  for (const roomId of list) {
    try {
      const payload = await api.reconnectRoom(Number(roomId))
      if (payload?.accepted) accepted += 1
      else declined += 1
    } catch (error) {
      failed.push(`${roomId}（${error.message}）`)
    }
  }
  const parts = []
  if (accepted) parts.push(`已请求重连 ${accepted} 个房间`)
  if (declined) parts.push(`${declined} 个当前不需要重连`)
  if (failed.length) parts.push(`${failed.length} 个失败：${failed.join('、')}`)
  showToast(parts.join('；') || '没有需要重连的房间')
  // 立刻刷一次健康与房间列表：让横幅/角标跟着新状态走，而不是等下一轮轮询。
  // 房间列表由 WS 的 `rooms` 控制消息维护，这里只需刷健康（它带 attention）。
  await refresh()
}

/**
 * 空状态横幅的「去添加房间」：把光标移到右侧的房间号输入框。
 *
 * 为什么不直接在横幅里放一个输入框：**同一个动作有两个输入位是坏设计** ——
 * 用户会分不清该填哪个，代码也要维护两套提交逻辑。房间号输入框本来就紧挨着
 * 它作用的那个列表（见 `RoomPanel.vue` 的排版说明），所以这里只负责**指过去**。
 */
function focusRoomInput() {
  roomPanelRef.value?.focusInput()
}

async function refresh() {
  const params =
    statsMode.value === 'cumulative'
      ? { room_id: roomId.value ?? undefined, window: 0 }
      : { room_id: roomId.value ?? undefined, window: statsWindow.value }
  try {
    stats.value = await api.stats(params)
    // 时间轴的统计起点 = **本次采集会话的起点**（不是直播间开播时间，也不是面板启动时刻）。
    // 只在累计口径下有意义 —— 滑动窗口口径下 `window_started_at` 是"窗口起点"，
    // 拿它当轴的下界会把轴缩成窗口长度，反而看不到趋势。
    if (statsMode.value === 'cumulative') {
      const hubStartedAt = stats.value?.cumulative?.started_at ?? stats.value?.window_started_at
      history.setStartTs(axisStartTs(hubStartedAt))
    }
  } catch {
    /* 统计失败不该打断弹幕流 */
  }
  try {
    health.value = await api.health()
  } catch {
    /* 同上 */
  }
  await refreshRoomInfo()
}

/**
 * 右侧栏顶部「XXX 的直播间」的数据来源。
 *
 * 已监控的房间直接用 `/api/stats` 里房间快照自带的 `anchor_name`（零额外请求）；
 * 只有"用户输入了但还没监控"的房间号才去查 `/api/room-info`。
 */
async function refreshRoomInfo() {
  const current = roomId.value
  if (current == null) {
    roomInfo.value = null
    return
  }
  const monitored = (rooms.value ?? []).find((room) => room.room_id === current)
  if (monitored?.anchor_name) {
    roomInfo.value = {
      room_id: monitored.room_id,
      short_id: monitored.short_id,
      anchor_uid: monitored.anchor_uid,
      anchor_name: monitored.anchor_name,
      anchor_face: monitored.anchor_face,
      live_status: monitored.live_status,
      title: `${monitored.anchor_name}的直播间`,
      monitored: true,
    }
    return
  }
  if (roomInfo.value?.room_id === current && roomInfo.value?.anchor_name) return
  try {
    roomInfo.value = await api.roomInfo(current)
  } catch {
    /* 房间信息取不到不该清空已有内容，也不该打断弹幕流 */
  }
}

async function addRoom(value) {
  if (adding.value) return
  adding.value = true
  try {
    const result = await api.addRoom(value)
    const resolved = result.room?.room_id ?? value
    showToast(`已开始监控房间 ${resolved}`)
    // 顺手把昵称记下来（需求 #8：下拉列表要显示主播昵称）
    rememberRoom(value, resolved, result.room?.anchor_name ?? '')
    roomId.value = result.room?.room_id ?? null
    await refresh()
  } catch (error) {
    showToast(`添加失败：${error.message}`)
  } finally {
    adding.value = false
  }
}

async function removeRoom(target) {
  try {
    await api.removeRoom(target)
    if (roomId.value === target) roomId.value = null
    await refresh()
  } catch (error) {
    showToast(`移除失败：${error.message}`)
  }
}

function exportData(format) {
  const params = { format, room_id: roomId.value ?? undefined }
  if (enabledTypes.value.length && enabledTypes.value.length < TYPE_ORDER.length) {
    params.type = enabledTypes.value.join(',')
  }
  if (keyword.value.trim()) params.q = keyword.value.trim()

  // ── 导出范围（需求 2026-10-09 第七轮）────────────────────────────────
  //
  // 规则（**按优先级从上到下**，与需求方口径一致）：
  //
  // 1. **用户显式选了某一次抓取**（右侧「只看某一次抓取」）⇒ 导出那一次会话的全部；
  //    ⚠️ 这一条必须排在 `startTs` 之前：过去那次会话的数据在 `startTs` **之前**，
  //    再套一层"本次采集起点"会把结果清空；
  // 2. **时间轴上选中了某个时段**（`history.window`，就是那条窗口带）⇒ 只导出该时段；
  //    这是"所见即所得"：用户在轴上框了 15 分钟，导出的就该是那 15 分钟；
  // 3. **其余情况**（回到实时 / 没在轴上选过）⇒ 导出**本次启动采集期间该房间**的弹幕，
  //    下界取轴下界 `startTs`（= `hub.started_at`，与时间轴画的那一段同源）。
  //
  // ⚠️ 刻意**不**再用 `history.range`（列表已加载的那一页）当范围：
  // 那是**派生量**（随弹幕密度漂，实测能差两个数量级）以前因为它错过一次。
  // 导出范围只认"用户的选择"（窗口）或"本次采集这个事实"（startTs）。
  const window_ = history.window.value
  const windowLo = Number(window_?.lo)
  const windowHi = Number(window_?.hi)
  if (history.sessionId.value) {
    params.session_id = history.sessionId.value
  } else if (Number.isFinite(windowLo) && Number.isFinite(windowHi) && windowHi > windowLo) {
    params.since = windowLo
    params.until = windowHi
  } else if (history.startTs.value > 0) {
    params.since = history.startTs.value
  }

  window.location.href = api.exportUrl(params)
}

/**
 * 导出**统计数据**（弹幕统计 + 每时段弹幕量，同一个文件里多张表）。
 *
 * 与 `exportData()` 的分工：
 *   * `exportData()` = 原始事件逐条，走 `window.location.href` 流式下载
 *     （数据量大，不能先进内存）；
 *   * `exportStats()` = 聚合统计，走 fetch + Blob —— **必须自己另存**，
 *     因为文件名要带"当天导出编号"，而那个编号只有前端能维护；
 *     `window.location.href` 用的是服务端 `Content-Disposition`，我们控制不了。
 *
 * 时段长度跟随时间轴当前选择（`history.bucketS`）；没选过就是 null，
 * 此时**不传** `bucket_s`，由后端按需求约定默认 5 分钟。
 *
 * 优先要 **xlsx**：同一次请求里后端会连 CSV 一起回（见 `server/app.py` 的
 * `format=xlsx` 说明）。要 xlsx 只是为了"一张表一个 sheet"更好读，
 * 所以后端没装 openpyxl（`xlsx` 为 `null`）时**静默退回 CSV** ——
 * 导出是用户明确点出来的动作，不该因为少个可选依赖就失败。
 */
async function exportStats() {
  if (exportingStats.value) return
  exportingStats.value = true
  const params = { room_id: roomId.value ?? undefined, format: 'xlsx' }
  if (enabledTypes.value.length && enabledTypes.value.length < TYPE_ORDER.length) {
    params.type = enabledTypes.value.join(',')
  }
  if (history.sessionId.value) params.session_id = history.sessionId.value
  const range = history.range.value
  if (history.browsing.value && range) {
    params.since = range.lo
    params.until = range.hi
  }
  const span = Number(history.bucketS.value)
  if (Number.isFinite(span) && span > 0) params.bucket_s = span

  try {
    const payload = await api.statsExport(params)
    const useXlsx = Boolean(payload.xlsx)
    const { day, seq } = nextExportSeq(localStorage)
    const filename = buildExportFilename({
      anchorName: payload.anchor_name || roomInfo.value?.anchor_name,
      roomId: payload.room_id ?? roomId.value,
      day,
      seq,
      ext: useXlsx ? 'xlsx' : 'csv',
    })
    if (useXlsx) {
      saveBinary(filename, base64ToBytes(payload.xlsx), XLSX_MIME)
    } else {
      saveBlob(filename, payload.csv ?? '', 'text/csv;charset=utf-8')
    }
    // 服务端在"格宽超出渲染预算"时会把格子抬粗（并用 `bucket_s_clamped` 标记）。
    // 这会让导出的时段表比用户选的更粗 —— 必须**说出来**，
    // 否则用户拿到一份粒度对不上的报表却不知道原因。
    showToast(
      payload.bucket_s_clamped
        ? `已导出 ${filename}（时段粒度为 ${formatBucketSpan(payload.bucket_s)}，超出细档预算已自动抬粗）`
        : `已导出 ${filename}`,
    )
  } catch (error) {
    showToast(`导出统计失败：${error.message}`)
  } finally {
    exportingStats.value = false
  }
}

/**
 * base64 → 字节数组。
 *
 * 实现在 `./base64.js`（独立成模块，便于单独校验）——
 * 这段转换写错会静默产出打不开的 xlsx，是最该小心的一块。
 */

/**
 * 把文本存成文件。
 *
 * 加 **UTF-8 BOM**：中文 Excel 只看文件头、忽略 Content-Type，
 * 没有 BOM 会按系统码页（GBK）解码成乱码 —— 这是本项目修过的缺陷 D19，
 * 面板的原始导出也一直这么做。
 */
function saveBlob(filename, text, mime) {
  const blob = new Blob([`\ufeff${text}`], { type: mime })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  // 立刻回收：Object URL 不释放会一直占着这份 Blob 的内存
  URL.revokeObjectURL(url)
}

/** xlsx 的 MIME。和 CSV 不同：它**不能**加 BOM（前 4 字节是 zip 魔数）。 */
const XLSX_MIME =
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

/** 存二进制内容（xlsx）。与 `saveBlob` 的唯一区别是**不加 BOM**。 */
function saveBinary(filename, bytes, mime) {
  const url = URL.createObjectURL(new Blob([bytes], { type: mime }))
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}

// --------------------------------------------------------------------------- //
// 复盘器（V3-a）：进入 / 退出历史浏览、翻页、定位
// --------------------------------------------------------------------------- //

/** 向上翻页：先记住当前高度，插入后补偿 `scrollTop`，否则视口会被顶走。 */
async function loadOlder() {
  const height = streamRef.value?.captureHeight() ?? 0
  await history.loadOlder()
  if (history.browsing.value) await streamRef.value?.preserveAnchor(height)
}

/**
 * 时间轴上点某一格：把窗口锚到那一格，**并把视口滚到那一刻那一条**。
 *
 * 为什么不是"滚到底"：合并视图里可能还混着更新的内容（贴住实时边缘时），
 * 滚到底会落在当下 —— 用户看到的就是"点了没反应"。所以按时间戳定位，
 * 定位不到才退化成滚到底（至少给个反馈，而不是停在原地）。
 *
 * ## Phase 1：这里只交出"锚点 + 段长"，其余全部由 `useHistory` 推导
 *
 * 以前这里会自己算 `span`、把 `ts` 原样交给 `history`、再拿同一个 `ts` 去滚主区 ——
 * **同一个"点的是哪一刻"被三处各用一次**，任何一处与画出来的格子不对齐，
 * 用户看到的就是"高亮时段与点击时段不匹配"。
 *
 * 现在：`history.jumpTo()` 内部把锚点吸附到格中心并写进窗口（唯一真相源），
 * 主区定位也**改读窗口的 `anchorTs`** —— 高亮与滚动用的是同一个值。
 */
async function jumpTo(payload) {
  const ts = typeof payload === 'number' ? payload : payload?.ts
  // ⚠️ 曲线的载荷是 `{ ts, spanS }`，柱状的是 `{ ts, bucketS }` —— **两个都要认**。
  //
  // 旧写法只读 `bucketS`，于是曲线的滚轮缩放 / 拖窗口把手算出来的段长在**这一层被丢掉**，
  // `span` 悄悄退回 `history.bucketS ?? 60`：用户拖了半天宽度，松手又弹回去
  // （需求方看到的"曲线不对劲"里有一条就是它）。
  const bucketS = typeof payload === 'number' ? undefined : (payload?.spanS ?? payload?.bucketS)
  if (!Number.isFinite(ts)) return
  const span = bucketS ?? history.bucketS.value ?? 60
  await history.jumpTo(ts, span)
  // 用**窗口里那个吸附后的锚点**去定位主区（而不是上面那个原始 ts）——
  // 否则"高亮在第 k 格、主区却滚到第 k 格之外的一刻"这种错位又会回来。
  const anchorTs = history.window.value?.anchorTs ?? ts
  //
  // ⚠️ **不要把 `span` 传进去**（2026-10-09 实测修正）：`jumpToTimestamp(ts, spanS)`
  // 的第二参是**桶宽**语义（"找 ts 落在 [row.ts, row.ts+span) 里的那一行"），
  // 而弹幕行是**事件级**时间戳 —— 一个 60 秒的窗口里有上百行都满足条件，
  // 函数返回的是**最早**那一行，于是弹幕区总是落在锚点**之前最多一个 span** 的地方。
  // 实测：播放头 23:22:55，弹幕区可视首条 23:21:53（差 62 秒 ≈ spanS）。
  // 传 0 ⇒ 走"离锚点最近的那一行"，才是"让我看到我点的那一刻"。
  const located = await streamRef.value?.jumpToTimestamp(anchorTs)
  if (!located) await streamRef.value?.jumpToBottom()
}

async function backToLive() {
  history.exit()
  // 窗口带**常驻**：回到实时后立刻把它放回数据末尾（不必等下一轮 6 秒刷新）。
  // 这里安全的原因是"同一个房间、同一份密度"——换房间那种场景不能这么调，
  // 那时 `density` 还是上一个房间的（改由 `bootTimeline()` 里那次 `loadDensity` 负责）。
  history.ensureLatestWindow()
  await streamRef.value?.jumpToBottom()
}

/** 加载会话列表（供"只看某次抓取"）。 */
async function loadSessions() {
  try {
    const payload = await api.sessions({ room_id: roomId.value ?? undefined, limit: 60 })
    sessions.value = payload.items ?? []
  } catch {
    sessions.value = []
  }
}

/**
 * 时间轴的**统计下界**（"本次采集"从哪一刻算起）。
 *
 * 规则与踩过的坑写在 `src/axisAnchor.js`（纯函数，有用例钉着）：
 * 取**库里最早那次采集会话**的起点，并以面板启动时刻封顶。
 */
function axisStartTs(hubStartedAt) {
  return anchorStart(hubStartedAt, sessions.value)
}

/**
 * 时间轴下界的**现场快照**，挂在 `.app` 的 `data-axis-start` 上。
 *
 * 为什么值得占一个属性：`/api/density` 请求由好几个入口触发（首屏、换房间、
 * 换类型、点时间轴跳转），"轴的下界到底是什么、来自哪一次会话"在浏览器里
 * 没有别的可观测出口 —— 而这一条正是"点一下时间轴、轴就换一条、滚动被打回 0"
 * 的核心变量。回归脚本（`tools/verify_timeline_highlight.py`）靠它取证。
 *
 * 格式：`startTs|hub=…|n=…|min=…|grid=lo,hi,span|anchor=…`
 * 后两段是 Phase 1 加上的：**判"高亮==点击"必须用数值**，不能拿"定位 12:44"
 * 这种格式化文案去反推（那会引入半格的误差，实测踩到过）。
 */
const axisStartInfo = computed(() => {
  const hub = Number(stats.value?.cumulative?.started_at ?? stats.value?.window_started_at ?? 0)
  const list = (sessions.value ?? [])
    .map((item) => Number(item?.started_at))
    .filter((value) => Number.isFinite(value) && value > 0)
  const start = history.startTs.value
  const range = history.density.value?.range ?? {}
  const span = Number(history.density.value?.bucket_s)
  const grid = `${Number(range.lo)};${Number(range.hi)};${Number.isFinite(span) ? span : ''}`
  const anchor = history.window.value?.anchorTs ?? ''
  return `${start}|hub=${hub}|n=${list.length}|min=${list.length ? Math.min(...list) : 0}`
    + `|grid=${grid}|anchor=${anchor}`
})

/**
 * 画时间轴：**先**定下界、**再**拉密度。
 *
 * 顺序是硬要求（实测踩到两次）：
 *
 * 1. **会话列表必须先到**。下界是"库里最早那次采集会话的起点"（见 `axisAnchor.js`），
 *    而会话列表是异步来的 —— 与 `refresh()` 并发的话，`refresh()` 会在
 *    `sessions.value` 还是空的时候就算出下界（退回面板启动时刻），
 *    轴于是被画短（实测：采集跨 2.75 h，轴只有 43 分钟）。
 * 2. **下界必须先于 `loadDensity()` 定下来**。`loadDensity()` 把它带进请求，
 *    并发的话首屏那条轴可能与之后每次重拉的**不是同一条** —— 轴一换，内容宽
 *    从 2250px 掉到 394px，浏览器把 `scrollLeft` 强制归 0，
 *    高亮的那一格就被甩出屏幕。
 */
async function bootTimeline() {
  await loadSessions()
  await refresh()
  // 下界（`startTs`）这时才定下来，画轴必须在它之后
  await history.loadDensity(enabledTypes.value)
}

async function onSelectSession(id) {
  await history.selectSession(id)
}

/**
 * 时间轴密度的**定时刷新**（需求方实测："曲线不会根据时间更新"）。
 *
 * 为什么必须专门刷一次：`loadDensity()` 的去重键只含"房间/会话/类型/粒度/since"，
 * **不含时间** —— 采集在继续、键却一直不变，所以从首屏那次之后它**一次都不会再拉**，
 * 曲线永远停在打开面板那一刻。这里用 `force: true` 显式绕过去重
 * （同参数的请求正在飞时仍会跳过，不会叠加）。
 *
 * 页面在后台时不刷：没人看，也没必要占 CPU（回到前台后下一轮就补上）。
 */
async function refreshDensity() {
  if (typeof document !== 'undefined' && document.hidden) return
  await history.loadDensity(enabledTypes.value, { force: true })
}

// --------------------------------------------------------------------------- //
// 感知分析（V3-b 3A）
// --------------------------------------------------------------------------- //

/**
 * 拉一次本地分析。
 *
 * 失败**静默处理**：分析只是辅助视图，不该因为它把面板搅得满屏报错
 * （`stats` / `health` 的失败也是这样处理的）。
 */
async function refreshAnalysis() {
  try {
    analysis.value = await api.analysis({ room_id: roomId.value ?? undefined, top: 20 })
  } catch {
    /* 分析失败不影响弹幕流 */
  }
}

/** 点词频榜里的词 → 复用顶栏的关键词过滤（不另造一套筛选状态）。 */
function filterByWord(word) {
  if (!word) return
  keyword.value = word
  keywordMode.value = 'filter'
}

// --------------------------------------------------------------------------- //
// 标记与区间导出（V3-a2）
// --------------------------------------------------------------------------- //

/** 标记"此刻"（可见的最后一条弹幕）。 */
async function addBookmark(payload) {
  await history.addBookmark({ recvTs: payload?.recvTs, label: payload?.label })
}

/** 标记某一条弹幕（钉在它上面；时间点仍取它的 recv_ts）。 */
async function markEvent(event) {
  if (!event) return
  await history.addBookmark({
    recvTs: Number(event.recv_ts),
    anchorId: event.id ?? event.seq ?? null,
  })
}

/** 按标记区间导出：终点交给后端取"下一个标记"。 */
function exportBookmarkRange({ bookmarkId, format }) {
  if (bookmarkId == null) return
  const params = { format, bookmark_id: bookmarkId }
  if (enabledTypes.value.length && enabledTypes.value.length < TYPE_ORDER.length) {
    params.type = enabledTypes.value.join(',')
  }
  if (keyword.value.trim()) params.q = keyword.value.trim()
  window.location.href = api.annotationExportUrl(params)
}

async function removeBookmark(id) {
  await history.removeBookmark(id)
}

async function clearBookmarks() {
  if (!history.bookmarks.value.length) return
  await history.clearBookmarks()
}

/**
 * 停止面板：请求服务端优雅退出（会先把 sink 队列 flush 落盘），再轮询等待进程消失。
 *
 * 这里**不用 `window.close()` 收尾**：非脚本打开的窗口，浏览器不允许关闭。
 * 所以停止成功后在页面上给出明确结果，标签页由用户自己关。
 */
async function stopPanel() {
  stopping.value = true
  try {
    await api.shutdown()
    showToast('已请求停止：正在 flush 落盘…')
  } catch (error) {
    stopping.value = false
    showToast(`停止失败：${error.message}`)
    return
  }

  const deadline = Date.now() + 30000
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 700))
    try {
      await api.health()
    } catch {
      showToast('面板已停止，数据已落盘。可以关闭此页面了。')
      return
    }
  }
  stopping.value = false
  showToast('等待超时：服务仍在运行，请用 `bili-danmaku web stop` 停止')
}

watch(roomId, () => {
  history.reset()
  history.exit()
  sessions.value = []
  reconnect()
  // 换房间同样要"先定轴的下界、再画轴"（见 bootTimeline 的说明）
  void bootTimeline()
  history.loadBookmarks()
  refreshAnalysis()
})

watch([statsMode, statsWindow], refresh)
// 类型过滤变化时刷新时间轴（密度条按类型聚合）
watch(enabledTypes, (types) => history.loadDensity(types))

let timer = null
let analysisTimer = null
let densityTimer = null
onMounted(() => {
  // ⚠️ **顺序要紧**：`refresh()` 会写 `history.setStartTs()`（时间轴的下界），
  // 而 `loadDensity()` 会把它带进请求。两者并发的话首次密度可能**不带 `since`** —
  // 于是首屏那条轴（"整库"口径）与之后每次重拉的轴（"本次采集"口径）**不是同一条**，
  // 用户点一下时间轴就会看到轴被换成短的那条、高亮的那一格被甩出屏幕。
  // 实测（真 Chrome）：点完约 1.5 s `scrollWidth` 从 2250 掉到 394、`scrollLeft` 归 0。
  void bootTimeline()
  // 标记存独立库：即使一条都没打过也要拉一次（库不存在时后端返回空列表，不是错误）
  history.loadBookmarks()
  refreshAnalysis()
  // 记忆里的房间补昵称（需求 #8）：只查库不联网，失败静默
  fillRoomHistoryNames()
  timer = setInterval(refresh, STATS_INTERVAL_MS)
  // 分析窗口是"最近 N 条"，刷新频率比统计低一档就够（它是给人读的，不是仪表盘）
  analysisTimer = setInterval(refreshAnalysis, ANALYSIS_INTERVAL_MS)
  // 时间轴密度要**自己**刷：去重键不含时间，不刷就永远停在首屏那一刻
  densityTimer = setInterval(refreshDensity, DENSITY_REFRESH_MS)
})
onUnmounted(() => {
  clearInterval(timer)
  clearInterval(analysisTimer)
  clearInterval(densityTimer)
  history.dispose()
})
</script>

<template>
  <div class="app" :data-axis-start="axisStartInfo">
    <TopBar
      :connected="connected"
      :received="received"
      :server-total="stats?.cumulative?.total ?? 0"
      :version="health?.version ?? ''"
      :uptime="uptime"
      :enabled-types="enabledTypes"
      :type-options="typeOptions"
      :type-counts="typeCounts"
      :keyword="keyword"
      :keyword-mode="keywordMode"
      :busy="stopping"
      :exporting-stats="exportingStats"
      :attention="health?.attention"
      @export="exportData"
      @export-stats="exportStats"
      @show-attention="attentionReveal += 1"
      @update:enabled-types="enabledTypes = $event"
      @update:keyword="keyword = $event"
      @update:keyword-mode="keywordMode = $event"
      @stop-panel="stopPanel"
    />

    <FilterBar
      :stats-mode="statsMode"
      :stats-window="statsWindow"
      :keyword-mode="keywordMode"
      :keyword="keyword"
      :filtered-count="visible.length"
      :total-count="scoped.length"
      :started-at="statsStartedAt"
      :browsing="history.browsing.value"
      :history-count="history.historyItems.value.length"
      :loading-history="history.loading.value"
      :sessions="sessions"
      :session-id="history.sessionId.value"
      :anonymize-name="anonymizeName"
      @update:stats-mode="statsMode = $event"
      @update:stats-window="statsWindow = $event"
      @update:anonymize-name="anonymizeName = $event"
      @enter-history="history.enter()"
      @back-to-live="backToLive"
      @select-session="onSelectSession"
    />

    <!-- 采集健康提示：放在 SC 横幅**之上**（它比 SC 更"需要你动手"）。
         数据搭 `/api/health` 的车，不额外轮询。 -->
    <HealthBanner
      :attention="health?.attention"
      :reveal-key="attentionReveal"
      @open-login="showLoginHint"
      @reconnect="reconnectRooms"
    />

    <!-- 空状态引导：一个房间都没有时，告诉用户"程序没问题，只差一个房间号"。
         放在健康横幅**之下** —— 健康横幅讲的是"已有房间出了问题"，
         一个房间都没有时它本来就该是 ok/不显示。 -->
    <EmptyRoomsBanner :room-count="rooms.length" @add-room="focusRoomInput" />

    <ScBanner :events="visible" />

    <div class="body">
      <div class="stream-column">
        <StreamList
          ref="streamRef"
          :events="visible"
          :keyword="keyword"
          :loading-older="history.loading.value"
          :has-older="history.hasOlder.value"
          :marked-stamps="markedStamps"
          :anonymize-name="anonymizeName"
          @near-top="loadOlder"
          @mark="markEvent"
        />
        <DensityBar
          :density="history.density.value"
          :selection="history.window.value"
          :loaded-range="history.range.value"
          :focus-ts="history.focusTs.value"
          :span-s="history.window.value?.spanS ?? null"
          @jump="jumpTo"
          @update:bucket="history.setBucketS($event)"
        />
        <BookmarkBar
          :bookmarks="history.bookmarks.value"
          :active-id="history.activeBookmarkId.value"
          :playhead-ts="playheadTs"
          :browsing="history.browsing.value"
          :busy="history.loading.value"
          @add="addBookmark"
          @jump="jumpTo"
          @select="history.activeBookmarkId.value = $event"
          @remove="removeBookmark"
          @clear="clearBookmarks"
          @export-range="exportBookmarkRange"
        />
      </div>
      <!-- 右侧信息栏：上方是"房间"（输入/添加/切换/直播间信息），下方是统计 -->
      <aside class="side">
        <RoomPanel
          ref="roomPanelRef"
          :current="roomId"
          :info="roomInfo"
          :rooms="rooms"
          :history="roomHistory"
          :busy="adding"
          :attention="health?.attention"
          @add="addRoom"
          @select="roomId = $event"
          @remove="removeRoom"
          @reconnect="reconnectRooms([$event])"
        />
        <StatsPanel :stats="stats" :current="roomId" />
        <AnalysisPanel
          :analysis="analysis"
          :all-rooms="roomId == null"
          @filter-word="filterByWord"
        />
      </aside>
    </div>

    <div
      v-if="toast"
      style="position:fixed;bottom:18px;right:18px;background:#1c1f26;border:1px solid var(--border);border-radius:8px;padding:10px 16px;box-shadow:0 6px 20px rgba(0,0,0,.5)"
    >
      {{ toast }}
    </div>

    <div
      v-if="errors.length"
      style="position:fixed;bottom:18px;left:18px;background:#3a1d1d;border:1px solid #7f1d1d;border-radius:8px;padding:8px 14px;font-size:12px"
    >
      <div v-for="(message, index) in errors" :key="index" class="warn">{{ message }}</div>
    </div>
  </div>
</template>
