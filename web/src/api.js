/** 与后端 REST 接口的薄封装。 */

function query(params = {}) {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    search.set(key, Array.isArray(value) ? value.join(',') : String(value))
  }
  return search.toString()
}

async function request(path, options = {}) {
  const response = await fetch(path, options)
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
  addRoom: (room) =>
    request('/api/rooms', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ room }),
    }),
  removeRoom: (roomId) => request(`/api/rooms/${roomId}`, { method: 'DELETE' }),
  /**
   * 房间号 → 房间/主播信息（含主播昵称）。
   *
   * 与 `addRoom` 分开：右侧栏要在用户**输入房间号时**就显示「XXX 的直播间」，
   * 而不是等他点了添加。查信息不启动采集。
   */
  roomInfo: (room) => request(`/api/room-info?${query({ room })}`),
  stats: (params = {}) => request(`/api/stats?${query(params)}`),
  events: (params = {}) => request(`/api/events?${query(params)}`),
  /**
   * 历史分页（V3-a）：**瘦投影 + 游标**。
   *
   * `params`：`room_id` / `session_id` / `type` / `q`，
   * 以及游标 `before_ts`+`before_id`（向更旧翻）或 `after_ts`+`after_id`（向更新翻）。
   * **两个游标键必须成对传**：`recv_ts` 会并列（同一批推送同刻），只传 `before_ts`
   * 会被后端当作"没有游标"，从而重复取回最新一页。
   */
  history: (params = {}) => request(`/api/history?${query(params)}`),
  /** 时间轴密度（服务端 SQL 聚合，别在前端算）。 */
  density: (params = {}) => request(`/api/density?${query(params)}`),
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
  statsExport: (params = {}) => request(`/api/stats/export?${query(params)}`),
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
