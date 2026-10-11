/**
 * 房间号下拉列表的**纯逻辑**（需求 #8）。
 *
 * 抽出来的理由与 `timeline.js` 一样：这段逻辑（记忆的规范化、按房间号/昵称过滤、
 * 两个来源的优先级）最容易在重构时悄悄改坏，而在 SFC 里就只能靠人点。
 * 组件只负责渲染，判断都在这里。
 */

/** 下拉列表最多显示几条：再多会盖住下面的「监控中」列表。 */
export const MAX_SUGGESTIONS = 8

/** 本地记忆里最多留几个房间。 */
export const MAX_ROOM_HISTORY = 12

/**
 * 昵称的**来源**，决定界面怎么标。
 *
 * 为什么要有这个枚举：需求方实测发现"房间号和主播昵称未匹配" —— 因为
 * 采集库推导的"留言最多的人"在热门房间里会选到**观众**（实测 4/4 全错）。
 * 所以权威值（正在监控的运行时昵称 / 向 B 站查到的 `uname`）与**猜测值**
 * 必须在数据层面就分开，界面才能如实标注，而不是把一个观众名显示成主播名。
 */
export const NAME_SOURCE = {
  /** 正在监控：运行时缓存里的真主播名，最准 */
  runtime: 'runtime',
  /** 权威：`/api/room-names?live=true` 返回的 `uname`（与"XXX 的直播间"同一接口） */
  live: 'live',
  /** 猜测：采集库推导的"留言最多的人" —— **可能不是主播**，界面必须标出来 */
  guess: 'guess',
}

/**
 * 规范化一条记忆记录。
 *
 * **兼容旧的纯字符串格式**：需求 #8 之前 `localStorage` 里存的是
 * `["804456", ...]`。读不了就等于把老用户的记忆清空了，所以两种形状都要认。
 * 返回 `null` 表示这条不能用（空值 / 形状不对）。
 */
export function normalizeRoomEntry(item) {
  if (item != null && typeof item === 'object') {
    const roomId = String(item.room_id ?? '').trim()
    if (!roomId) return null
    const name = String(item.anchor_name ?? '')
    // `name_source` 如实记下"这个名字是怎么来的"，界面据此决定要不要标"据库推测"
    const source = String(item.name_source ?? (name ? NAME_SOURCE.guess : ''))
    return { room_id: roomId, anchor_name: name, name_source: source }
  }
  const roomId = String(item ?? '').trim()
  if (!roomId) return null
  return { room_id: roomId, anchor_name: '', name_source: '' }
}

/** 规范化整个记忆列表：去重、截断到 `MAX_ROOM_HISTORY`。 */
export function normalizeRoomHistory(raw, limit = MAX_ROOM_HISTORY) {
  if (!Array.isArray(raw)) return []
  const seen = new Set()
  const out = []
  for (const item of raw) {
    const entry = normalizeRoomEntry(item)
    if (!entry || seen.has(entry.room_id)) continue
    seen.add(entry.room_id)
    out.push(entry)
    if (out.length >= limit) break
  }
  return out
}

/**
 * 合并成下拉列表的候选：**本地记忆 ∪ 已监控房间**。
 *
 * 昵称优先级（**权威 > 猜测**，这是"房间号和昵称不匹配"的修法）：
 *
 *   1. **正在监控**的房间 → 运行时缓存里的 `anchor_name`（`source=runtime`，真主播名）；
 *   2. 记忆里 `name_source=live` → 之前向 B 站查到的**权威**昵称；
 *   3. 记忆里只有名字、来源不明/`guess` → 采集库推导的"留言最多的人"，
 *      **很可能不是主播**（实测热门房间 4/4 全错），所以标成 `guess`，
 *      界面只显示"曾采集"而不把它当主播名。
 *
 * 排序与原来一致：房间号大的排前面（近似"最近用过的"），并列时按字典序稳定。
 */
export function buildSuggestions({ history = [], rooms = [] } = {}) {
  const byId = new Map()
  for (const item of history) {
    const entry = normalizeRoomEntry(item)
    if (!entry) continue
    byId.set(entry.room_id, {
      room_id: entry.room_id,
      anchor_name: entry.anchor_name,
      name_source: entry.name_source,
      monitored: false,
    })
  }
  for (const room of rooms) {
    if (!room?.room_id) continue
    byId.set(String(room.room_id), {
      room_id: String(room.room_id),
      anchor_name: String(room.anchor_name ?? ''),
      name_source: NAME_SOURCE.runtime,
      monitored: true,
    })
  }
  return [...byId.values()].sort(
    (a, b) => Number(b.room_id) - Number(a.room_id) || a.room_id.localeCompare(b.room_id),
  )
}

/**
 * 按输入过滤候选。
 *
 * **房间号和昵称都匹配**：用户可能只记得"那个叫什么的主播"，
 * 只按房间号过滤会让他找不到 —— 而昵称正是这次要加进来用的信息。
 */
export function filterSuggestions(list, query, limit = MAX_SUGGESTIONS) {
  const needle = String(query ?? '').trim().toLowerCase()
  if (!needle) return list.slice(0, limit)
  return list
    .filter(
      (item) =>
        item.room_id.includes(needle) ||
        String(item.anchor_name || '').toLowerCase().includes(needle),
    )
    .slice(0, limit)
}
