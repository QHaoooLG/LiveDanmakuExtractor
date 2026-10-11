/** 与后端 REST 接口的薄封装。 */

function query(params = {}) {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    search.set(key, Array.isArray(value) ? value.join(',') : String(value))
  }
  return search.toString()
}

/**
 * 各端点的客户端超时（毫秒）。
 *
 * 为什么前端也要有自己的超时：后端确实给房间解析加了兜底，
 * 但**事件循环被慢查询堵住**时后端根本来不及回任何东西
 * （实测 `/api/health` 从 21 ms 变成 2412 ms，那还是最轻的一个端点）。
 * 没有客户端超时，`RoomPanel` 的 `busy` 就会一直是 true，
 * 输入框和按钮全被禁着 —— 用户看到的就是"面板卡死"，而且**永远不会自己恢复**。
 *
 * 取值原则：**略大于后端的兜底**，让后端那条更具体的错误信息（504 + 中文原因）
 * 先到达；客户端超时是"后端彻底没反应"时的最后一道保险。
 */
export const TIMEOUT_MS = {
  /** 缺省：只读的轻量端点（health / rooms / sessions / annotations …）。 */
  default: 15000,
  /** 房间解析与批量昵称：后端兜底 12 s（`api.resolve_timeout`）+ 网络余量。 */
  interactive: 20000,
  /** 历史分页 / 密度：冷库上服务端聚合可能真的要几秒。 */
  query: 30000,
  /** 统计导出（xlsx 要现算 + base64），这是全项目最重的同步计算。 */
  export: 90000,
}

/**
 * 发一个请求，**带超时**。
 *
 * `options.timeoutMs` 是本次请求的超时（毫秒，`0` 表示不限）；其余键原样交给
 * `fetch`（它不认识 `timeoutMs`，所以必须在这里摘掉再传）。
 *
 * 超时时抛的是**中文的 Error**：`fetch` 因 `AbortController` 中断时抛的是
 * `AbortError: The operation was aborted`，直接透出去用户完全看不懂，
 * 还会被上层当成"未知错误"。
 *
 * `options.signal` 是**调用方自己的取消信号**（2026-10-09 第六轮加的）：
 * 时间轴连点粒度档位时用它取消上一条 `/api/density` —— 见 `useHistory.queueDensity()`。
 * 两条信号（调用方的 + 超时的）都接在内部那个 controller 上，所以能同时生效；
 * 但**抛出的错必须能区分**：调用方主动取消是正常路径，不该弹"请求超时"。
 */
export async function request(path, options = {}) {
  const { timeoutMs = TIMEOUT_MS.default, signal: externalSignal, ...init } = options
  const controller = typeof AbortController === 'function' ? new AbortController() : null
  let timer = null
  let onExternalAbort = null
  if (controller && externalSignal) {
    if (externalSignal.aborted) controller.abort()
    else {
      onExternalAbort = () => controller.abort()
      externalSignal.addEventListener('abort', onExternalAbort)
    }
  }
  if (controller && timeoutMs > 0) {
    timer = setTimeout(() => controller.abort(), timeoutMs)
  }

  let response
  try {
    response = await fetch(path, controller ? { ...init, signal: controller.signal } : init)
  } catch (error) {
    if (controller?.signal.aborted) {
      if (externalSignal?.aborted) {
        const cancelled = new Error(`请求已取消：${path}`)
        cancelled.name = 'AbortError' // 调用方据此**静默**跳过（不是故障）
        throw cancelled
      }
      const seconds = Math.round(timeoutMs / 1000)
      throw new Error(`请求超时（${seconds}s）：${path} —— 服务可能正忙或网络不通，请稍后重试`)
    }
    throw error
  } finally {
    if (timer !== null) clearTimeout(timer)
    if (onExternalAbort && externalSignal) externalSignal.removeEventListener('abort', onExternalAbort)
  }

  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`
    try {
      const body = await response.json()
      if (body?.detail) detail = body.detail
    } catch {
      /* 非 JSON 错误体，保留状态码 */
    }
    throw new Error(detail)
  }
  if (response.status === 204) return null
  return response.json()
}

export const api = {
  health: () => request('/api/health'),
  rooms: () => request('/api/rooms'),
  /**
   * 添加房间。超时给到 `interactive`：这条走的是**真实采集启动**
   * （`_resolve_room` + 建 task），比只查信息重一点。
   */
  addRoom: (room) =>
    request('/api/rooms', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ room }),
      timeoutMs: TIMEOUT_MS.interactive,
    }),
  removeRoom: (roomId) => request(`/api/rooms/${roomId}`, { method: 'DELETE' }),
  /**
   * **手动重连**一个房间（需求 2026-10-09）。
   *
   * 自动重连连续失败到上限后采集器会**刻意停下**（无限重连只会对已经出问题的
   * 服务端一直打请求），这时要有人能推一把。响应里的 `accepted` 表示"是否真的
   * 排了一次重连"：当前连接正常时会返回 `accepted: false` + 原因，
   * **那不是错误**（是好消息），所以这里不当异常抛。
   */
  reconnectRoom: (roomId) => request(`/api/rooms/${roomId}/reconnect`, { method: 'POST' }),
  /**
   * 房间号 → 房间/主播信息（含主播昵称）。
   *
   * 与 `addRoom` 分开：右侧栏要在用户**输入房间号时**就显示「XXX 的直播间」，
   * 而不是等他点了添加。查信息不启动采集。
   *
   * 走 `interactive` 超时（不是缺省的 15 s）：系统代理不通时后端要等到
   * 自己的 12 s 兜底才回 504，把更具体的原因让给后端。
   */
  roomInfo: (room) =>
    request(`/api/room-info?${query({ room })}`, { timeoutMs: TIMEOUT_MS.interactive }),
  /**
   * 批量取房间昵称（供房间号下拉列表显示"曾采集"的房间）。
   *
   * `live: true` 会**顺带向 B 站查权威主播昵称**（每个房间一次请求）。
   * 两级来源是刻意的：先要不联网的库推导值让列表秒出，再拿权威值覆盖 ——
   * 库推导的"留言最多的人"在热门房间里可能是**观众**（实测 4/4 全错）。
   * 返回值里的 `guessed` 列出"这个名字是猜的"房间号，界面据此如实标注。
   */
  roomNames: (rooms, { live = false } = {}) =>
    request(
      `/api/room-names?${query({ rooms: (rooms ?? []).join(','), live: live || undefined })}`,
      { timeoutMs: TIMEOUT_MS.interactive },
    ),
  stats: (params = {}) => request(`/api/stats?${query(params)}`, { timeoutMs: TIMEOUT_MS.query }),
  events: (params = {}) => request(`/api/events?${query(params)}`, { timeoutMs: TIMEOUT_MS.query }),
  /**
   * 历史分页（V3-a）：**瘦投影 + 游标**。
   *
   * `params`：`room_id` / `session_id` / `type` / `q`，
   * 以及游标 `before_ts`+`before_id`（向更旧翻）或 `after_ts`+`after_id`（向更新翻）。
   * **两个游标键必须成对传**：`recv_ts` 会并列（同一批推送同刻），只传 `before_ts`
   * 会被后端当作"没有游标"，从而重复取回最新一页。
   *
   * `signal` 用来取消上一次请求（与 `density` 同款）。拖动时间轴时每次
   * `pointermove` 都会跳转一次，不取消的话会堆出上百个并发请求
   * —— 每个都在服务端查一次库、在浏览器里解析一页 300 条。
   */
  history: (params = {}, { signal } = {}) =>
    request(`/api/history?${query(params)}`, { timeoutMs: TIMEOUT_MS.query, signal }),
  /** 时间轴密度（服务端 SQL 聚合，别在前端算）。`signal` 用来取消上一次请求。 */
  density: (params = {}, { signal } = {}) =>
    request(`/api/density?${query(params)}`, { timeoutMs: TIMEOUT_MS.query, signal }),
  /** 抓取会话列表（"只看某一次抓取"）。 */
  sessions: (params = {}) => request(`/api/sessions?${query(params)}`),
  /**
   * 本地感知分析（V3-b 3A）：词频 / 热点突增 / 疑似刷屏 / 疑似异常账号。
   *
   * 数据源是后端**内存窗口**，不是数据库 —— 它回答的是"现在大家在聊什么"，
   * 与 `/api/stats`（累计口径）、`/api/density`（历史分布）是三件事。
   * 全部本地计算，**不会有任何数据离开本机**。
   */
  analysis: (params = {}) => request(`/api/analysis?${query(params)}`),
  /**
   * 标注 / 标记（V3-a2）：存**独立库**，与采集库无关。
   *
   * `bookmark_id` 只作为区间**起点**：不给终点时，后端自动取"下一个标记"
   * （没有则到该房间数据末尾）—— 这是"从这一步导到下一步"的直觉语义。
   */
  annotations: (params = {}) => request(`/api/annotations?${query(params)}`),
  addAnnotation: (body) =>
    request('/api/annotations', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  removeAnnotation: (id) => request(`/api/annotations/${id}`, { method: 'DELETE' }),
  annotationExportUrl: (params = {}) => `/api/annotations/export?${query(params)}`,
  exportUrl: (params = {}) => `/api/export?${query(params)}`,
  /**
   * **统计数据**：概要 / 类型分布 / 每时段弹幕量 / 发言 TOP。
   *
   * 与 `exportUrl`（原始事件逐条流式下载）是两件事，两个都保留：
   * 前者回答"这段时间发生了什么"，后者是"把原始数据给我"。
   *
   * 这里走 JSON 而不是直接 `window.location.href` —— 因为文件名的
   * "当天导出编号"只有前端能维护，必须由前端另存（见 `preferences.js`）；
   * 也正因为走 JSON，xlsx 只能 base64 塞进字段里（`format: 'xlsx'`）。
   */
  statsExport: (params = {}) =>
    request(`/api/stats/export?${query(params)}`, { timeoutMs: TIMEOUT_MS.export }),
  /**
   * 停止面板：服务端会先 flush 落盘再退出。
   *
   * 这是本项目唯一的「管理类」接口，服务端只接受**本机来源**
   * （见 `server/app.py` 的控制端点说明）；用 POST 而不是 GET，
   * 因为"GET 不产生副作用"这条约定值得守住。
   */
  shutdown: () => request('/api/admin/shutdown', { method: 'POST' }),
}

export { query }
