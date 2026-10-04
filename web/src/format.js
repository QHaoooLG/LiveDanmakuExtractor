/** 展示层格式化。所有函数都是纯函数，便于单独推理与测试。 */

const TYPE_LABEL = {
  danmaku: '弹幕',
  super_chat: 'SC',
  gift: '礼物',
  guard_buy: '上舰',
  interact: '互动',
  like: '点赞',
  room_stats: '房间',
  live_status: '状态',
  notice: '公告',
  dm_interaction: '互动',
  other: '其他',
}

export const TYPE_ORDER = [
  'danmaku',
  'super_chat',
  'gift',
  'guard_buy',
  'interact',
  'like',
  'notice',
  'room_stats',
  'dm_interaction',
  'other',
]

/**
 * 每种弹幕类型一种颜色 —— 与 `style.css` 里的 `--type-*` 变量一一对应。
 *
 * 统一在这里维护的理由：弹幕行的高亮、类型分布柱状图、类型多选面板
 * 三处都要用同一套颜色，分散写一定会漂移。
 */
const TYPE_COLOR = {
  danmaku: '#8ab4f8',
  super_chat: '#f472b6',
  gift: '#fbbf24',
  guard_buy: '#a78bfa',
  interact: '#64748b',
  like: '#34d399',
  notice: '#94a3b8',
  room_stats: '#38bdf8',
  dm_interaction: '#22d3ee',
  other: '#6b7280',
}

export function typeColor(type) {
  return TYPE_COLOR[type] ?? 'var(--type-other)'
}

export function typeLabel(type) {
  return TYPE_LABEL[type] ?? type
}

/** 时间戳（秒）→ HH:MM:SS */
export function formatTime(seconds) {
  if (!seconds) return '--:--:--'
  const date = new Date(seconds * 1000)
  const pad = (n) => String(n).padStart(2, '0')
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

export function formatDuration(seconds) {
  if (seconds == null) return '-'
  const total = Math.max(0, Math.round(seconds))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  if (h) return `${h}h${m}m`
  if (m) return `${m}m${s}s`
  return `${s}s`
}

/**
 * 该条的身份是否被**平台**打码（B 站未登录时下发 `宅***` 这类昵称）。
 *
 * 只看 `uname` 里有没有 `***`，**不看 uid**：
 * 之前写成 `if (!event?.uid) return true`，那是错的 —— `uid=0` 是"被平台打码的匿名
 * 用户"（项目在 `analysis.py` / `filters.py` 里专门区分过 `uid is not None`），
 * 把 `uid=0` 一律当"打码"会让这些行被标成灰色斜体，看着像出错了。
 * 真实昵称 + uid=0 是完全可能的（匿名但昵称可见），那种行不该被标记。
 */
export function isMasked(event) {
  return Boolean(event?.uname && event.uname.includes('***'))
}

export function displayName(event) {
  return event?.uname || '匿名'
}

/**
 * 本地匿名化：只留昵称的**首字符**，其余用 `*` 掩盖。
 *
 * 与"平台打码"是两件不同的事，不要混：
 *   * **平台打码**是 B 站下发的 `宅***`（未登录时会出现），本地**无法还原**；
 *   * **本函数**是用户主动打开的显示偏好，把**真实昵称**就地遮起来，
 *     用于录屏/截图/共享屏幕时不想暴露他人 ID 的场景。
 *
 * 用 `Array.from` 而不是 `name[0]`：昵称可能有 emoji（代理对），
 * `name[0]` 会把一个 emoji 拆成半个乱码。
 * 掩盖长度按**字符数**给足，但不泄露原长（固定再多补 3 个，避免"数星号猜长度"）。
 */
export function maskName(name) {
  const text = (name ?? '').toString()
  if (!text) return ''
  const chars = Array.from(text)
  const first = chars[0]
  const rest = Math.min(Math.max(chars.length - 1, 0), 8) + 3
  return `${first}${'*'.repeat(rest)}`
}

/** 该事件的主要展示文本。 */
export function eventText(event) {
  const extra = event?.extra ?? {}
  switch (event?.type) {
    case 'gift': {
      const name = extra.gift_name || event.text || '礼物'
      const num = extra.num ?? extra.total_num
      return num && num > 1 ? `${name} ×${num}` : name
    }
    case 'super_chat':
      return `¥${event.price ?? '?'} ${event.text ?? ''}`.trim()
    case 'guard_buy':
      return `${extra.gift_name || '上舰'}（等级 ${extra.guard_level ?? event.guard_level ?? '?'}）`
    case 'interact':
      return extra.interact_label || extra.msg_type_label || '进入直播间'
    case 'like':
      return extra.like_text || event.text || '点赞了'
    case 'room_stats':
      if (extra.watched_text) return `看过 ${extra.watched_text}`
      if (extra.online_count != null) return `在线 ${extra.online_count}`
      if (extra.fans != null) return `粉丝 ${extra.fans}`
      return '房间数据更新'
    case 'dm_interaction':
      return event.text || '互动'
    case 'notice':
      return event.text || '系统公告'
    case 'live_status':
      return event.text || event.cmd || '直播状态'
    case 'other':
      return event.cmd || '未知事件'
    default:
      return event?.text ?? ''
  }
}

/** 礼物 / SC / 上舰是否值得高亮展示。 */
export function isPriced(event) {
  return ['gift', 'super_chat', 'guard_buy'].includes(event?.type)
}

export function medalText(event) {
  const medal = event?.medal
  if (!medal || (!medal.name && !medal.level)) return ''
  return `${medal.name ?? ''}${medal.level ?? ''}`
}

const EMOT_PATTERN = /\[([A-Za-z0-9_\-\u4e00-\u9fff ]{1,20})\]/g

/**
 * 把正文切成「文本段 / 表情段」。
 *
 * B 站的弹幕表情是内嵌占位符（`哈哈[dog]`），图片链接在 `extra.emots` 里
 * （后端随事件一起下发）。渲染成图片而不是把 `[dog]` 原样显示，面板观感才对得上直播间。
 *
 * 只接受 http(s) 的图片地址：弹幕正文是**不可信输入**，
 * 不能让 `javascript:` 之类混进 `<img src>`。
 */
export function textSegments(event) {
  const value = eventText(event) ?? ''
  const emots = event?.extra?.emots
  const mapping = emots && typeof emots === 'object' ? emots : {}
  const out = []
  let cursor = 0

  for (const match of value.matchAll(EMOT_PATTERN)) {
    const meta = mapping[match[0]]
    const url = meta?.url
    if (typeof url !== 'string' || !/^https?:\/\//i.test(url)) continue

    if (match.index > cursor) out.push({ type: 'text', text: value.slice(cursor, match.index) })
    out.push({ type: 'emot', text: match[0], url, alt: match[1] })
    cursor = match.index + match[0].length
  }

  if (cursor < value.length) out.push({ type: 'text', text: value.slice(cursor) })
  return out.length ? out : [{ type: 'text', text: value }]
}
