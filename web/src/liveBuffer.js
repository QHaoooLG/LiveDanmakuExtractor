/**
 * 实时事件缓冲的**纯逻辑**（无 Vue、无 WebSocket）。
 *
 * 从 `useLiveStream` 里抽出来有两个原因：
 *
 *  1. **可测**：`useLiveStream` 一挂载就连 WebSocket，在 node 环境里没法直接测；
 *     而"合并发布 + 就地裁剪"的正确性恰恰是最需要钉住的部分
 *     （它决定界面显示的是不是"最新的 N 条"）。
 *  2. **说明白**：合并发布是改造 F 第 2 项，单独一个文件更容易讲清
 *     "为什么可以攒 100ms 再发布"。
 *
 * ## 语义（与改造前的 `concat` + `slice` 逐字一致）
 *
 * 任何时刻 `flush()` 之后暴露的数组 = **最新的 min(已收到, limit) 条**，
 * 且顺序为"旧 → 新"。攒批只改变**什么时候**发布，不改变发布出来的内容。
 */

/**
 * @param {object} options
 * @param {number} options.limit 缓冲上限（保留最新的这么多条）
 * @param {number} options.intervalMs 合并发布间隔（毫秒）
 * @param {(events: unknown[]) => void} options.publish 发布回调（收到新数组时调用）
 * @param {typeof setTimeout} [options.setTimeoutImpl] 便于测试注入
 * @param {typeof clearTimeout} [options.clearTimeoutImpl]
 */
export function createLiveBuffer({
  limit,
  intervalMs,
  publish,
  setTimeoutImpl = setTimeout,
  clearTimeoutImpl = clearTimeout,
}) {
  let capacity = limit
  let pending = []
  let timer = null
  let current = []
  let disposed = false

  function publishNow() {
    // 每次都换一个新数组：上层用的是 `shallowRef`，靠引用变化触发依赖方
    publish(current.slice())
  }

  /** 把 `pending` 合并进当前缓冲，并裁到上限。**只分配一次数组。** */
  function flush() {
    // ⚠️ 定时器句柄必须**无条件**清掉：`setLimit` / `clear` / `dispose` 会
    // 主动调 `flush()`，此时 `pending` 可能已经是空的。若因为有早退分支而
    // 把 `timer` 留着，稍后那次定时器还会再触发一遍 —— 虽然内容被裁过、
    // 再发布一次也不会错，但那是白跑一次下游的整条 computed 链。
    if (timer !== null) {
      clearTimeoutImpl(timer)
      timer = null
    }
    if (!pending.length) return
    const incoming = pending
    pending = []

    // 等价于改造前的 `cur.concat(incoming)` 再 `slice(-limit)`，
    // 但先把新事件推进去、再就地左移裁掉老的 —— 省掉一次整数组拷贝。
    // 用 `for` 而非展开：单批可能上千条，展开会撞参数个数上限。
    for (let i = 0; i < incoming.length; i += 1) current.push(incoming[i])
    if (current.length > capacity) current.splice(0, current.length - capacity)
    publishNow()
  }

  function schedule() {
    if (timer !== null || disposed) return
    timer = setTimeoutImpl(flush, intervalMs)
  }

  return {
    /** 收一条（或一批）事件；到点才发布。 */
    push(event) {
      if (disposed) return
      pending.push(event)
      // 上限护栏：万一定时器被节流（标签页在后台），也不能无限攒。
      // 攒的比 `capacity` 还多没有意义 —— 多出来的那些注定会被裁掉。
      if (pending.length > capacity) pending.splice(0, pending.length - capacity)
      schedule()
    },

    /** 立刻发布（切换房间、调整上限、卸载前都要先落地）。 */
    flush,

    /** 调整上限：先把待发布的并进来，再裁到新上限。 */
    setLimit(next) {
      capacity = typeof next === 'number' && next > 0 ? Math.floor(next) : limit
      // `flush()` 会把 pending 并进来（并在过程中裁到新上限）
      flush()
      if (current.length > capacity) current.splice(0, current.length - capacity)
      // 无条件发布一次：调用方（进入历史浏览）期待**立刻**看到收缩后的数组，
      // 而 `flush()` 在 pending 为空时不会发布。
      publishNow()
    },

    /** 清空（断线/切房间）。 */
    clear() {
      // 先清掉待发布内容，再 `flush()` —— 这样 `flush` 只负责取消定时器，
      // 不会把即将被丢弃的 pending 先发布出去（那样会多触发一次下游重算）。
      pending = []
      flush()
      current = []
      publishNow()
    },

    /** 卸载：停掉定时器，避免它在组件销毁后还去写 ref。 */
    dispose() {
      disposed = true
      if (timer !== null) clearTimeoutImpl(timer)
      timer = null
      pending = []
    },

    /** 当前已发布的内容（测试用）。 */
    snapshot() {
      return current.slice()
    },
  }
}
