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

import { createLiveBuffer } from '../liveBuffer.js'

const MAX_EVENTS = 2000
const MAX_ERRORS = 5
const BASE_RETRY_MS = 800
const MAX_RETRY_MS = 15000

/**
 * 实时事件的**合并发布间隔**（毫秒）。
 *
 * 为什么需要它：`events` 是 `shallowRef`，而 `App.vue` 里有一串基于它的
 * `computed`（`allEvents` / `availableTypes` / `typeCounts` / `scoped`…），
 * 每次换数组都会让它们**整体重算**。原实现是"来一条就换一次数组"
 * （`concat` + `slice` 两次 2000 槽拷贝），于是每条弹幕都要付一次全链路重算。
 *
 * 合并成最多 10 次/秒之后：**渲染内容不变**（界面上 100 ms 内到的事件
 * 本来就画在同一帧里），但下游重算次数降到 1/10 以下。
 *
 * 缓冲逻辑本身在 `liveBuffer.js` 里（纯函数、可单独测）。
 */
const FLUSH_INTERVAL_MS = 100

export function useLiveStream(roomIdRef) {
  const connected = ref(false)
  const rooms = shallowRef([])
  /** 最新事件在数组尾部 */
  const events = shallowRef([])
  const errors = shallowRef([])
  const received = ref(0)

  //: 事件缓冲（合并发布 + 就地裁剪），逻辑见 `liveBuffer.js`
  const buffer = createLiveBuffer({
    limit: MAX_EVENTS,
    intervalMs: FLUSH_INTERVAL_MS,
    publish: (next) => {
      events.value = next
    },
  })

  let socket = null
  let retryTimer = null
  let retryCount = 0
  let disposed = false

  /**
   * 调整缓冲上限：立即裁掉超出部分（保留最新的）。
   * 传 `undefined` 恢复默认。
   */
  function setLimit(next) {
    buffer.setLimit(next)
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
        buffer.push(payload.data)
        break
      }      case 'error':
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
    buffer.clear()
    retryCount = 0
    received.value = 0
    if (socket) {
      socket.onclose = null
      socket.close()
    }
    connect()
  }

  function clear() {
    buffer.clear()
    received.value = 0
  }

  connect()

  onUnmounted(() => {
    disposed = true
    clearTimeout(retryTimer)
    buffer.dispose()
    if (socket) {
      socket.onclose = null
      socket.close()
    }
  })

  return { connected, rooms, events, errors, received, reconnect, clear, setLimit }
}
