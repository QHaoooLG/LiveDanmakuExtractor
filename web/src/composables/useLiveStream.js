/**
 * 实时弹幕流：管理 WebSocket 连接、重连与内存缓冲。
 *
 * 过滤（类型 / 关键词）放在**客户端**做：服务端也支持过滤，但那样每次勾选
 * 都要重连、并丢掉已缓冲的内容。房间切换才需要重连（回填按房间取）。
 *
 * V3-a 起缓冲上限可**运行时收缩**（`setLimit`）：进入历史浏览时把实时层收到 300 条，
 * 否则"历史窗口 + 持续增长的实时流"会让合并视图无限膨胀。
 * 注意这是"临时降级"，回实时后要能调回 2000。
 */
import { onUnmounted, ref, shallowRef } from 'vue'

const MAX_EVENTS = 2000
const MAX_ERRORS = 5
const BASE_RETRY_MS = 800
const MAX_RETRY_MS = 15000

export function useLiveStream(roomIdRef) {
  const connected = ref(false)
  const rooms = shallowRef([])
  /** 最新事件在数组尾部 */
  const events = shallowRef([])
  const errors = shallowRef([])
  const received = ref(0)
  /** 当前缓冲上限（默认 MAX_EVENTS，历史浏览时会被调小） */
  let limit = MAX_EVENTS

  let socket = null
  let retryTimer = null
  let retryCount = 0
  let disposed = false

  /**
   * 调整缓冲上限：立即裁掉超出部分（保留最新的）。
   * 传 `undefined` 恢复默认。
   */
  function setLimit(next) {
    limit = typeof next === 'number' && next > 0 ? Math.floor(next) : MAX_EVENTS
    const current = events.value
    if (current.length > limit) events.value = current.slice(current.length - limit)
  }

  function pushError(message) {
    errors.value = [...errors.value, message].slice(-MAX_ERRORS)
  }

  function applyRooms(next) {
    if (Array.isArray(next)) rooms.value = next
  }

  function handle(payload) {
    switch (payload.t) {
      case 'hello':
        applyRooms(payload.data?.rooms)
        break
      case 'rooms':
        applyRooms(payload.data?.rooms)
        break
      case 'event': {
        received.value += 1
        const next = events.value.concat(payload.data)
        events.value = next.length > limit ? next.slice(next.length - limit) : next
        break
      }
      case 'error':
        pushError(payload.data?.hint ?? payload.data?.code ?? '未知错误')
        break
      default:
        break // ping / stats 等控制消息不影响数据流
    }
  }

  function scheduleRetry() {
    if (disposed) return
    const delay = Math.min(BASE_RETRY_MS * 2 ** retryCount, MAX_RETRY_MS)
    retryCount += 1
    clearTimeout(retryTimer)
    retryTimer = setTimeout(connect, delay)
  }

  function connect() {
    if (disposed) return
    const params = new URLSearchParams()
    if (roomIdRef.value) params.set('room_id', String(roomIdRef.value))

    const scheme = location.protocol === 'https:' ? 'wss' : 'ws'
    socket = new WebSocket(`${scheme}://${location.host}/ws/live?${params}`)

    socket.onopen = () => {
      connected.value = true
      retryCount = 0
      errors.value = []
    }
    socket.onmessage = (event) => {
      try {
        handle(JSON.parse(event.data))
      } catch {
        /* 非法帧忽略即可 */
      }
    }
    socket.onclose = () => {
      connected.value = false
      scheduleRetry()
    }
    socket.onerror = () => socket?.close()
  }

  /** 切换房间时调用：清空缓冲并重连（历史回填按房间取）。 */
  function reconnect() {
    clearTimeout(retryTimer)
    retryCount = 0
    events.value = []
    received.value = 0
    if (socket) {
      socket.onclose = null
      socket.close()
    }
    connect()
  }

  function clear() {
    events.value = []
    received.value = 0
  }

  connect()

  onUnmounted(() => {
    disposed = true
    clearTimeout(retryTimer)
    if (socket) {
      socket.onclose = null
      socket.close()
    }
  })

  return { connected, rooms, events, errors, received, reconnect, clear, setLimit }
}
