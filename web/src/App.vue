<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { api } from './api.js'
import { useHistory } from './composables/useHistory.js'
import { useLiveStream } from './composables/useLiveStream.js'
import { TYPE_ORDER, typeLabel } from './format.js'
import {
  buildExportFilename,
  loadAnonymizeName,
  nextExportSeq,
  saveAnonymizeName,
} from './preferences.js'
import AnalysisPanel from './components/AnalysisPanel.vue'
import BookmarkBar from './components/BookmarkBar.vue'
import DensityBar from './components/DensityBar.vue'
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
const TOAST_MS = 4000
const SC_THRESHOLD_KEY = 'bili-danmaku.sc-threshold'
const KEYWORD_MODE_KEY = 'bili-danmaku.keyword-mode'
const STATS_MODE_KEY = 'bili-danmaku.stats-mode'
const STATS_WINDOW_KEY = 'bili-danmaku.stats-window'
const ROOM_HISTORY_KEY = 'bili-danmaku.room-history'
const MAX_ROOM_HISTORY = 12
const DEFAULT_SC_THRESHOLD = 30
const DEFAULT_STATS_WINDOW = 300

const roomId = ref(null)
const enabledTypes = ref([...TYPE_ORDER])
const keyword = ref('')
const keywordMode = ref(localStorage.getItem(KEYWORD_MODE_KEY) || 'highlight')
const statsMode = ref(localStorage.getItem(STATS_MODE_KEY) || 'cumulative')
const statsWindow = ref(Number(localStorage.getItem(STATS_WINDOW_KEY)) || DEFAULT_STATS_WINDOW)
const scThreshold = ref(loadNumber(SC_THRESHOLD_KEY, DEFAULT_SC_THRESHOLD))
/**
 * 是否匿名化弹幕流的发言者昵称（只留首字符）。
 *
 * 状态放在这里（而不是 StreamList 内部）是因为**开关的位置在 FilterBar**
 * —— 两个组件各存一份必然会不同步。默认**不**匿名。
 */
const anonymizeName = ref(loadAnonymizeName(localStorage))
watch(anonymizeName, (value) => saveAnonymizeName(localStorage, value))
/** 正在导出统计 CSV（按钮置灰防连点） */
const exportingStats = ref(false)
const stats = ref(null)
const health = ref(null)
const toast = ref('')
const stopping = ref(false)
const adding = ref(false)
/** `/api/room-info` 的结果：右侧栏顶部的「XXX 的直播间」 */
const roomInfo = ref(null)
/** 本地记忆过的房间号（最近输入/添加过的排前面） */
const roomHistory = ref(loadRoomHistory())
/** 抓取会话列表（"只看某一次抓取"） */
const sessions = ref([])
/** 本地感知分析（V3-b 3A）：词频 / 热点突增 / 疑似刷屏 / 疑似异常账号 */
const analysis = ref(null)
/** 实时缓冲上限：进入历史浏览后收缩，避免合并视图无限增长 */
const liveLimit = ref(null)

const streamRef = ref(null)

function loadNumber(key, fallback) {
  const saved = Number(localStorage.getItem(key))
  return Number.isFinite(saved) && saved >= 0 ? saved : fallback
}

/**
 * 本地记忆的房间号。
 *
 * 只存"用户主动输入/添加过"的房间号（不是服务端返回的房间列表）：
 * 目的是下次打开面板时不用重新敲 —— 服务端没在监控的房间号也要记得住，
 * 这正是"记忆"和"已监控列表"的区别。解析成真实房间号后**替换掉**
 * 原来的短号，避免记忆里同时留下短号和真实号。
 */
function loadRoomHistory() {
  try {
    const raw = JSON.parse(localStorage.getItem(ROOM_HISTORY_KEY) || '[]')
    return Array.isArray(raw) ? raw.map(String).filter(Boolean).slice(0, MAX_ROOM_HISTORY) : []
  } catch {
    return []
  }
}

function rememberRoom(value, resolved) {
  const key = String(resolved ?? value).trim()
  if (!key) return
  const next = [key, ...roomHistory.value.filter((item) => item !== key && item !== String(value))]
  roomHistory.value = next.slice(0, MAX_ROOM_HISTORY)
  localStorage.setItem(ROOM_HISTORY_KEY, JSON.stringify(roomHistory.value))
}

// 这几个偏好存本地：刷新页面后不用重新调
watch(scThreshold, (value) => localStorage.setItem(SC_THRESHOLD_KEY, String(value)))
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
 * 历史条目来自 `/api/history` 的瘦投影：主键在顶层 `id`、表情在顶层 `emots`。
 * 实时事件是完整 `Event`：主键在 `seq`（后端 `store._to_event()` 把 `id` 映射成 `seq`）、
 * 表情在 `extra.emots`。合并视图要按 `id` 去重、按同一套逻辑渲染，
 * 所以在这里统一一次 —— **不要**在渲染层分散写兼容分支。
 */
function unify(event) {
  if (event.id != null) return event
  return { ...event, id: event.seq }
}

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

async function refresh() {
  const params =
    statsMode.value === 'cumulative'
      ? { room_id: roomId.value ?? undefined, window: 0 }
      : { room_id: roomId.value ?? undefined, window: statsWindow.value }
  try {
    stats.value = await api.stats(params)
    // 时间轴的统计起点 = **本次面板启动时刻**（需求：不要按直播间开播时间画轴）。
    // 只在累计口径下有意义 —— 滑动窗口口径下 `window_started_at` 是"窗口起点"，
    // 拿它当轴的下界会把轴缩成窗口长度，反而看不到趋势。
    if (statsMode.value === 'cumulative') {
      history.setStartTs(stats.value?.cumulative?.started_at ?? stats.value?.window_started_at)
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
    rememberRoom(value, resolved)
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
  // 历史浏览中导出当前窗口的时间范围（所见即所得），否则导出整个房间
  const range = history.range.value
  if (history.browsing.value && range) {
    params.since = range.lo
    params.until = range.hi
  }
  if (history.sessionId.value) params.session_id = history.sessionId.value
  window.location.href = api.exportUrl(params)
}

/**
 * 导出**统计 CSV**（弹幕统计 + 每时段弹幕量，同一个文件里多张表）。
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
 */
async function exportStats() {
  if (exportingStats.value) return
  exportingStats.value = true
  const params = { room_id: roomId.value ?? undefined }
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
    const { day, seq } = nextExportSeq(localStorage)
    const filename = buildExportFilename({
      anchorName: payload.anchor_name || roomInfo.value?.anchor_name,
      roomId: payload.room_id ?? roomId.value,
      day,
      seq,
    })
    saveBlob(filename, payload.csv ?? '', 'text/csv;charset=utf-8')
    showToast(`已导出 ${filename}`)
  } catch (error) {
    showToast(`导出统计失败：${error.message}`)
  } finally {
    exportingStats.value = false
  }
}

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

// --------------------------------------------------------------------------- //
// 复盘器（V3-a）：进入 / 退出历史浏览、翻页、定位
// --------------------------------------------------------------------------- //

/** 向上翻页：先记住当前高度，插入后补偿 `scrollTop`，否则视口会被顶走。 */
async function loadOlder() {
  const height = streamRef.value?.captureHeight() ?? 0
  await history.loadOlder()
  if (history.browsing.value) await streamRef.value?.preserveAnchor(height)
}

/** 时间轴上点某一格：把窗口锚到那一格，并把视口放到该页底部（那里正是所点时刻）。 */
async function jumpTo(payload) {
  const ts = typeof payload === 'number' ? payload : payload?.ts
  const bucketS = typeof payload === 'number' ? undefined : payload?.bucketS
  if (!Number.isFinite(ts)) return
  await history.jumpTo(ts, bucketS ?? history.bucketS.value ?? 60)
  await streamRef.value?.scrollToBottom()
}

async function backToLive() {
  history.exit()
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

async function onSelectSession(id) {
  await history.selectSession(id)
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
  refresh()
  loadSessions()
  history.loadDensity(enabledTypes.value)
  history.loadBookmarks()
  refreshAnalysis()
})

watch([statsMode, statsWindow], refresh)
// 类型过滤变化时刷新时间轴（密度条按类型聚合）
watch(enabledTypes, (types) => history.loadDensity(types))

let timer = null
let analysisTimer = null
onMounted(() => {
  refresh()
  loadSessions()
  // 首屏就画出时间轴：不进入历史浏览也能看到"今天什么时候热闹"
  history.loadDensity(enabledTypes.value)
  // 标记存独立库：即使一条都没打过也要拉一次（库不存在时后端返回空列表，不是错误）
  history.loadBookmarks()
  refreshAnalysis()
  timer = setInterval(refresh, STATS_INTERVAL_MS)
  // 分析窗口是"最近 N 条"，刷新频率比统计低一档就够（它是给人读的，不是仪表盘）
  analysisTimer = setInterval(refreshAnalysis, ANALYSIS_INTERVAL_MS)
})
onUnmounted(() => {
  clearInterval(timer)
  clearInterval(analysisTimer)
  history.dispose()
})
</script>

<template>
  <div class="app">
    <TopBar
      :connected="connected"
      :received="received"
      :version="health?.version ?? ''"
      :uptime="uptime"
      :enabled-types="enabledTypes"
      :type-options="typeOptions"
      :type-counts="typeCounts"
      :keyword="keyword"
      :keyword-mode="keywordMode"
      :sc-threshold="scThreshold"
      :busy="stopping"
      :exporting-stats="exportingStats"
      @export="exportData"
      @export-stats="exportStats"
      @update:enabled-types="enabledTypes = $event"
      @update:keyword="keyword = $event"
      @update:keyword-mode="keywordMode = $event"
      @update:sc-threshold="scThreshold = $event"
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
    <HealthBanner :attention="health?.attention" @open-login="showLoginHint" />

    <ScBanner :events="visible" :threshold="scThreshold" />

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
          :range="history.range.value"
          :focus-ts="history.focusTs.value"
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
          :current="roomId"
          :info="roomInfo"
          :rooms="rooms"
          :history="roomHistory"
          :busy="adding"
          @add="addRoom"
          @select="roomId = $event"
          @remove="removeRoom"
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
