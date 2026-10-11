/**
 * 时间轴的**统计下界**（"本次采集从哪一刻画起"）—— 纯函数，便于离线钉死。
 *
 * ## 规则：只覆盖「本次启动采集」
 *
 * 取**本次运行开出来的那些采集会话**的起点，与面板启动时刻取较小者。
 * 以前运行留下的会话**不算**（那是"历史"，要用右侧的"只看某一次抓取"去看）。
 *
 * ## 为什么不是"库里最早的那次会话"（2026-10-09 实测回归，务必先读）
 *
 *以前把这里改成"库里**最早**那次会话的起点"，本意是修
 * "面板启动晚于采集时、轴被画短"。但那个场景只存在于**离线构造的数据**里
 * （造出来的会话全在面板启动之前），真实产品里"面板就是采集器"。
 * 于是它对真实房间造成了**更严重**的破坏 —— 实测（房间 721 = 真实房间号
 * 3044248，库里自 9/30 起有历史，而本次采集刚开始）：
 *
 * | 指标 | 实测值 |
 * |------|--------|
 * | 轴跨度 | **8.85 天**（9/30 00:15 → 10/8 20:41） |
 * | 自适应粒度 | **2 小时/格**（7200 秒） |
 * | 曲线点数 | **3 个**（8.85 天里只有 3 个非空桶 ⇒ 画不出曲线） |
 * | 刻度标签 | 8 个**全都是 `08:00:00`** |
 *
 * 需求要的是："时间轴只统计**本次启动采集**之后的
 * 数据" —— "一次 10 分钟的采集，时间轴却横跨好几天，每格都被压成看不见的细线"
 * 正是这条需求当初要消灭的现象。所以恢复本次运行口径。
 *
 * ## 为什么要 `RUN_TOLERANCE_S`
 *
 * 面板自己开的那次会话，`started_at` 可能比 `hub.started_at` **早几十秒**
 * （实测：hub=08:41:30、面板自己的会话=08:41:00；另一处实测是晚几秒）。
 * 那是同一"次"采集，必须认；而"上次运行"至少是几分钟/几天前。
 * 取 180 秒既容得下这个错位，又不会把上一次运行捞回来。
 */

/** 面板自己的会话与 `hub.started_at` 允许的错位（秒）。见文件头说明。 */
export const RUN_TOLERANCE_S = 180

/**
 * @param {number} hubStartedAt 面板启动时刻（unix 秒）
 * @param {Array<{started_at?: number|string}>} sessions 采集会话列表（`/api/sessions`）
 * @param {{toleranceS?: number}} [options]
 * @returns {number} 时间轴的下界（unix 秒）；0 表示"取不到任何下界"
 */
export function axisStartTs(hubStartedAt, sessions, { toleranceS = RUN_TOLERANCE_S } = {}) {
  const hub = Number(hubStartedAt)
  const hasHub = Number.isFinite(hub) && hub > 0
  const starts = (sessions ?? [])
    .map((item) => Number(item?.started_at))
    .filter((value) => Number.isFinite(value) && value > 0)

  if (!starts.length) return hasHub ? hub : 0

  // 只认"本次运行"的会话。没有面板启动时刻（老接口/接口挂了）时无从判断新旧，
  // 就退回旧行为（全都要）——**宁可多画一点，也不要把轴画成空的**。
  const runStarts = hasHub
    ? starts.filter((value) => value >= hub - Math.max(Number(toleranceS) || 0, 0))
    : starts
  if (!runStarts.length) return hub

  return hasHub ? Math.min(hub, ...runStarts) : Math.min(...runStarts)
}
