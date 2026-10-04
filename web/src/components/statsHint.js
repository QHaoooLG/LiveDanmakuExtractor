/**
 * 「发言 TOP」为空时的说明文案。
 *
 * 抽成纯函数是为了能离线单测 —— 这段逻辑踩过一次真坑（见下），
 * 放在 SFC 里就只能靠人眼看。
 *
 * ⚠️ **不能用 `source === 'memory'` 判断"sqlite sink 没开"**：
 * `source` 描述的是**条数/速率**来自 hub 内存计数，而累计口径（面板默认）
 * **永远**是 `'memory'`；但"发言 TOP"其实来自 SQLite 的累计查询。
 * 于是新库上会提示"启用 sqlite sink 后才有发言榜"，而 sqlite 默认**本来就开着** ——
 * 用户照着提示去做也解决不了。
 *
 * 所以改成按**后端给的事实**分情况（`/api/stats` 的累计分支）：
 *   * 有 `top_senders_scope`            → 真的查过库，只是没有数据；
 *   * `top_senders_error === '数据库尚未建立'` → 全新安装/还没采到数据，**不是故障**；
 *   * `top_senders_error === 'sqlite sink 未启用'` → 这才是用户要去改配置的情况。
 */

export function topSendersHint(stats) {
  const senders = stats?.top_senders ?? []
  if (senders.length) return ''

  if (stats?.mode !== 'cumulative') return '该范围内暂无发言'

  if (stats != null && 'top_senders_scope' in stats) {
    return '库里还没有可聚合的发言（开始采集后就会出现）'
  }

  const reason = stats?.top_senders_error
  if (reason === 'sqlite sink 未启用') return '未启用 sqlite sink（在配置的 sinks.types 里打开）'

  if (reason === '数据库尚未建立') {
    // 全新安装时这个榜本来就是空的 —— 只有"面板明明收到了弹幕、库里却还是没有"，
    // 才值得提示（那说明落盘可能有问题）。否则只是噪音。
    const msgs = stats?.msgs_total ?? 0
    return msgs > 0 ? '收到弹幕但采集库还没建立，检查数据目录是否可写' : ''
  }

  // 兜底：后端没给原因（旧版本后端），只说"暂无"，不误导用户去改 sqlite
  return '暂无发言榜数据'
}
