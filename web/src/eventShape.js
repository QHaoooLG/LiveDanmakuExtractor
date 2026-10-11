/**
 * 实时事件 → 与历史投影**同形**的条目。
 *
 * 历史条目来自 `/api/history` 的瘦投影：主键在顶层 `id`、表情在顶层 `emots`。
 * 实时事件是完整 `Event`：主键在 `seq`、表情在 `extra.emots`。
 * 合并视图（`useHistory`）要按 `id` 去重、按同一套逻辑渲染，
 * 所以在这一处统一 —— **不要**在渲染层分散写兼容分支。
 */

/**
 * 记忆化表。用 `WeakMap` 而不是 `Map`：键是事件对象，
 * 条目随事件被回收而自动消失，**不会**把已经滑出缓冲的事件钉在内存里。
 */
const unifiedCache = new WeakMap()

/**
 * 把一条实时事件补齐成同形条目（幂等：历史条目本身就带 `id`，原样返回）。
 *
 * ⚠️ **必须记忆化**（2026-10-10 的记忆化改造）：
 *
 * `useLiveStream` 每来一条弹幕就换一个**新数组**（`events.value = next`），
 * 所以 `App.vue` 里那个 `computed(() => liveEvents.value.map(unify))`
 * 会**每条事件**重算一次。而实时事件的 `id` 恒为 `undefined`
 * （后端广播的字段是 `seq`，没有 `id` —— 见 `server/hub.py`），
 * 于是 `event.id != null` 这条快速路径**永远不命中**。
 *
 * 结果：每条新弹幕都会把整个缓冲（最多 2000 条）浅拷贝一遍 ——
 * 约 2000 个新对象 / 条弹幕，按 10 msg/s 就是约 2 万对象/秒的纯垃圾。
 * 记忆化之后，每个事件对象**一生只被包装一次**。
 */
export function unify(event) {
  if (event.id != null) return event
  let wrapped = unifiedCache.get(event)
  if (wrapped === undefined) {
    wrapped = { ...event, id: event.seq }
    unifiedCache.set(event, wrapped)
  }
  return wrapped
}
