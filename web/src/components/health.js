/**
 * 健康横幅的**判定逻辑**（需求：把凭证失效/重连异常做得更显眼）。
 *
 * 抽成纯函数有两个理由，都是实打实的：
 *
 * 1. **可离线测**：这段逻辑（"该不该显示""要不要给去登录""是不是同一个问题"）
 *    在 SFC 里只能靠挂载后点界面验证；而挂载 + 点关闭 + 回读 DOM 这一串
 *    在 `node --test` 里会把堆吃光（实测 `FATAL ERROR: Committing semi space
 *    failed`）。逻辑提出来之后就是普通函数调用，稳且快。
 * 2. **一处判定**：横幅、顶栏角标、房间列表的 ⚠ 都靠它，三处不会各说各话。
 */

/**
 * 一条问题（`assess_health()` 的结果）的**稳定标识**。
 *
 * 用它判断"是不是同一个问题"：同一个问题关掉之后不该再弹，换了问题就该重新弹。
 * 优先用后端给的 `code`（机器可读），**不要去正则匹配 `title`** ——
 * 那种写法一改文案就会坏（`code` 是契约，文案不是）。
 */
export function attentionKey(payload) {
  if (!payload || payload.level === 'ok' || !payload.title) return ''
  return `${payload.level}|${payload.code || payload.title}|${(payload.rooms ?? []).join(',')}`
}

/** 该不该显示横幅（`null` = 不显示）。 */
export function visibleAttention(payload) {
  if (!payload || payload.level === 'ok' || !payload.title) return null
  return payload
}

/**
 * 横幅当前是否可见（`dismissedKey` 是用户关掉的那条问题的标识）。
 *
 * `revealNonce` 变化 = 用户点了顶栏角标，要求**强制展开**（覆盖关闭记忆）。
 * 所以判定里不比较 nonce 的值，只看"关掉的那条是不是当前这条"。
 */
export function isBannerVisible(payload, dismissedKey, { revealed = false } = {}) {
  const visible = visibleAttention(payload)
  if (!visible) return false
  if (revealed) return true
  return attentionKey(visible) !== dismissedKey
}

/**
 * 要不要给「去登录」按钮。
 *
 * **优先用后端的 `action`**（`login` / `retry`）：这是机器可读的契约。
 * 只有在旧后端没给 `action` 时才退回"看级别 + 文案"的兜底 ——
 * 网络问题**不能**引导用户去重新登录（那是白折腾一趟，第一版就踩过这个坑）。
 */
export function needsRelogin(payload) {
  const visible = visibleAttention(payload)
  if (!visible) return false
  if (visible.action) return visible.action === 'login'
  return visible.level === 'alert' && /登录|凭证|认证/.test(visible.title)
}

/**
 * 要不要给「手动重连」按钮（需求 2026-10-09）。
 *
 * 自动重连连续失败到上限后采集器会停下等人，这时**唯一能推进的动作**就是
 * 点一下重连 —— 所以这个按钮必须出现，而且必须显眼（它不是一个"也可以"的选项）。
 *
 * 与 `needsRelogin` 一样，判据优先用后端的 `action`（机器可读契约），
 * 不解析文案。两者**可能同时为真**（登录失效又达上限），那时两个按钮都给：
 * 先登录再重连是有效路径，只给一个会让人来回试。
 */
export function needsReconnect(payload) {
  const visible = visibleAttention(payload)
  if (!visible) return false
  if (visible.action) return visible.action === 'reconnect'
  return visible.code === 'waiting_manual'
}

/**
 * 顶栏常驻角标的文案；`null` = 不显示。
 *
 * 与横幅的区别：**角标不可关**。横幅能被点掉，而"点掉之后又忘了"正是这类提示
 * 最典型的失效方式 —— 所以只要问题还在，角标就一直亮着。
 */
export function attentionBadge(payload) {
  const visible = visibleAttention(payload)
  if (!visible) return null
  const isAlert = visible.level === 'alert'
  return {
    level: visible.level,
    text: isAlert ? '⚠ 需要处理' : '⚠ 连接异常',
    title: [visible.title, visible.detail, '点这里展开完整提示'].filter(Boolean).join('\n'),
  }
}
