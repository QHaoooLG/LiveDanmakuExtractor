/**
 * 复盘器（V3-a）：**历史浏览层**。
 *
 * 面板原来的数据流是单层的：`/ws/live` → 环形缓冲 2000 → 过滤 → 渲染 400。
 * 一旦滚出缓冲，历史数据就"看得见摸不着"（数据其实全在 SQLite 里）。
 *
 * 这一层的职责就是把历史接上，并且**刻意不动实时层**：
 *
 *   * 实时层继续管"最近 2000 条 + 贴底自动滚动"，行为不变；
 *   * 历史层按 `(recv_ts, id)` 游标向 SQLite 翻页，独立于实时缓冲；
 *   * 合并视图按 `id` 去重、按 `(recv_ts, id)` 排序 —— 两层的时间范围可能重叠，
 *     去重必须靠 `id`（`session_id + seq` 在跨会话时不可比）。
 *
 * 两个关键设计约束：
 *   1. **不靠扩缓冲解决问题**。"把 2000 提到 50000"既崩内存又崩渲染，
 *      而且翻不到上周三那场直播。历史必须来自存储层。
 *   2. **进入历史浏览后要收窄实时缓冲**。否则合并视图 = 历史窗口 + 持续增长的实时流，
 *      渲染量会失控 —— 这是当初把缓冲设成 2000 的原因，不能在这里破功。
 */

import { computed, ref, shallowRef } from 'vue'
import { api } from '../api.js'

/** 历史窗口上限（合并视图里历史部分最多留这么多条） */
const HISTORY_CAP = 1200
/** 单页条数 */
export const PAGE_SIZE = 300
/** 进入历史浏览后，实时缓冲收缩到的条数（限制合并规模） */
export const LIVE_TAIL_WHEN_HISTORY = 300

export function useHistory(roomIdRef, liveEvents, liveLimit, onError) {
  /** 历史窗口（升序）。空数组 = 未进入历史浏览 */
  const historyItems = shallowRef([])
  const browsing = ref(false)
  const loading = ref(false)
  /** 服务端返回的"这一页更旧/更新方向还有没有数据" */
  const pageHasOlder = ref(false)
  const pageHasNewer = ref(false)
  /** 窗口是否已离开实时边缘（向上翻过页，或时间轴定位过） */
  const detached = ref(false)
  /** 是否已经确认翻到了最早一条（拿到过空页 ⇒ 再往上真的没有了） */
  const exhausted = ref(false)
  const windowS = ref(0)
  const sessionId = ref(null)
  /** 时间轴密度（与当前房间/会话/类型过滤一致） */
  const density = ref(null)
  /** 正在定位的时间点（用于高亮密度条） */
  const focusTs = ref(null)
  /** 标记列表（按时间升序；存独立库，与采集库无关） */
  const bookmarks = shallowRef([])
  /** 标记里"钉住"哪一条（用于导出区间与 UI 高亮） */
  const activeBookmarkId = ref(null)
  /**
   * 时间轴每格时长（秒）。用户可选 1/3/5/10/15 分钟（`DensityBar` 的档位）。
   *
   * `null` = **自适应**：首屏还不知道数据跨度时让后端按跨度挑一个人类可读的步长，
   * 拿到结果后把实际步长记回这里（`density.bucket_s`），后续用户切换就固定用它。
   */
  const bucketS = ref(null)
  /**
   * 时间轴的**统计起点**（unix 秒；0 = 不限制）。
   *
   * 需求：时间轴只从**本次启动采集的时刻**开始统计，而不是从直播间的开播时间。
   * 不设这个下界时，`/api/density` 会按库里该房间的**全部历史**（`time_range()`）
   * 画轴 —— 于是一次 10 分钟的采集，时间轴却横跨好几天，
   * 每格都被压成一根看不见的细线，悬停信息看起来也就"很乱"。
   *
   * 由 `App.vue` 从 `/api/stats` 的 `window_started_at`（= `hub.started_at`）写入。
   */
  const startTs = ref(0)

  function setStartTs(value) {
    const next = Number(value)
    startTs.value = Number.isFinite(next) && next > 0 ? next : 0
  }

  let disabled = false
  let requestSeq = 0

  function fail(error) {
    if (typeof onError === 'function') onError(error)
  }

  function roomParams() {
    const params = { room_id: roomIdRef.value ?? undefined }
    if (sessionId.value) params.session_id = sessionId.value
    return params
  }

  /** 当前历史窗口的时间范围（合并视图与密度条的高亮都靠它） */
  const range = computed(() => {
    const items = historyItems.value
    if (!items.length) return null
    return { lo: items[0].recv_ts, hi: items[items.length - 1].recv_ts }
  })

  /**
   * 还有没有更旧的数据。
   *
   * `detached` 时不能只看服务端的 `has_older`：窗口被 `HISTORY_CAP` 截断后，
   * 之前那页的 `has_older` 会随着窗口尾部数据被丢弃而失真。
   * 但也不能永远为真 —— 拿到过空页（`exhausted`）就说明真的到最早了。
   */
  const hasOlder = computed(
    () => browsing.value && !exhausted.value && (pageHasOlder.value || detached.value),
  )

  /**
   * 还有没有更新的数据。
   *
   * **不能直接用服务端的 `has_newer`**：它只说明"这一页之后还有没有数据"。
   * 用户翻上去几页之后，窗口尾部已经不含最新数据，服务端对这页算出的
   * `has_newer` 会是 false —— 如果照抄，就再也翻不回最新了（实测踩到过）。
   * 正确语义是"**窗口是否还没贴住实时边缘**"。
   */
  const hasNewer = computed(() => browsing.value && detached.value)

  const oldest = computed(() => {
    const items = historyItems.value
    return items.length ? { ts: items[0].recv_ts, id: items[0].id } : null
  })

  const newest = computed(() => {
    const items = historyItems.value
    return items.length ? { ts: items[items.length - 1].recv_ts, id: items[items.length - 1].id } : null
  })

  function adopt(page, { replace = false, prepend = false } = {}) {
    const incoming = page.items ?? []
    const next = replace ? [...incoming] : prepend ? [...incoming, ...historyItems.value] : [...historyItems.value, ...incoming]
    // 按 id 去重（历史页之间、以及历史与实时之间都可能重叠）
    const seen = new Set()
    const deduped = []
    for (const item of next) {
      if (seen.has(item.id)) continue
      seen.add(item.id)
      deduped.push(item)
    }
    // 超出上限时，从**远离焦点的一侧**截断：prepend 说明在看更早的 → 丢尾部
    historyItems.value =
      deduped.length > HISTORY_CAP
        ? prepend
          ? deduped.slice(0, HISTORY_CAP)
          : deduped.slice(deduped.length - HISTORY_CAP)
        : deduped
    pageHasOlder.value = Boolean(page.has_older)
    pageHasNewer.value = Boolean(page.has_newer)
    windowS.value = page.window_s ?? 0
    // 空页 = 这个方向真的到头了（`before` 方向没数据 ⇒ 已到最早）
    if (!incoming.length) {
      if (prepend) exhausted.value = true
      if (!prepend && page.has_newer === false && !replace) detached.value = false
    } else {
      // 拿到了真实数据：如果这一页还指向更旧的方向，说明没到头，解除"已翻完"标记
      if (page.has_older) exhausted.value = false
      // 首屏（无游标）落在实时边缘：贴住新的那一端
      if (!prepend && !page.has_newer) detached.value = false
    }
  }

  /** 拉取一页。`cursor` 为空 = 首屏（最新一页）。 */
  async function fetchPage(cursor = {}, { replace = false, prepend = false } = {}) {
    if (disabled) return
    const ticket = ++requestSeq
    loading.value = true
    try {
      const page = await api.history({ ...roomParams(), limit: PAGE_SIZE, ...cursor })
      if (ticket !== requestSeq) return // 已有更新的请求，丢弃这一份
      adopt(page, { replace, prepend })
    } catch (error) {
      fail(`加载历史失败：${error.message}`)
    } finally {
      if (ticket === requestSeq) loading.value = false
    }
  }

  /** 进入历史浏览：从最新一页开始。 */
  async function enter() {
    if (browsing.value) return
    browsing.value = true
    focusTs.value = null
    liveLimit.value = LIVE_TAIL_WHEN_HISTORY
    await Promise.all([fetchPage({}, { replace: true }), loadDensity()])
  }

  /** 回到实时：清空历史，实时层恢复原有缓冲规模。 */
  function exit() {
    browsing.value = false
    historyItems.value = []
    pageHasOlder.value = false
    pageHasNewer.value = false
    detached.value = false
    exhausted.value = false
    focusTs.value = null
    liveLimit.value = null
    requestSeq += 1
    loading.value = false
  }

  /** 向上翻（更旧）。翻过页就等于离开了实时边缘。 */
  async function loadOlder() {
    if (!browsing.value || loading.value || !hasOlder.value || !oldest.value) return
    const from = oldest.value
    await fetchPage({ before_ts: from.ts, before_id: from.id }, { prepend: true })
    // 只要窗口起点真的往前走了，就算"离开实时边缘"
    if (historyItems.value.length && historyItems.value[0].id < from.id) detached.value = true
  }

  /** 向下翻（更新，直到重新贴住实时边缘）。 */
  async function loadNewer() {
    if (!browsing.value || loading.value || !hasNewer.value || !newest.value) return
    await fetchPage({ after_ts: newest.value.ts, after_id: newest.value.id })
    // 往新的一侧走过之后，"已到最早"的结论就不一定还成立了
    exhausted.value = false
    // 服务端说"这一页之后没有更多了" ⇒ 已经回到实时边缘
    if (!pageHasNewer.value) detached.value = false
  }

  /**
   * 跳到某个时间点（点时间轴的某一格）。
   *
   * **锚点必须落在所点格子内**，否则"我点了这格、高亮的却是前一格"。
   * 曾经的做法是 `before_ts = ts`：那取到的是"该时刻之前的 300 条"，
   * 窗口右端正好落在**所点格子的起点**上，于是整段窗口落在上一格里 ——
   * 这就是实测反馈的 off-by-one。
   *
   * 现在用 `until = 该格结束时刻`（闭区间）划出窗口右端，
   * 窗口左端就会落在所点的这一格内；`bucketS` 由时间轴按当前缩放传入。
   */
  async function jumpTo(ts, bucketS = 60) {
    browsing.value = true
    focusTs.value = ts
    detached.value = true
    exhausted.value = false
    liveLimit.value = LIVE_TAIL_WHEN_HISTORY
    const bucketEnd = Number(ts) + Math.max(Number(bucketS) || 60, 1)
    await Promise.all([
      fetchPage(
        { until: bucketEnd, before_ts: bucketEnd, before_id: Number.MAX_SAFE_INTEGER },
        { replace: true },
      ),
      loadDensity(),
    ])
  }

  /** 时间轴密度（按当前房间/会话/类型过滤 + 用户选的格宽）。 */
  async function loadDensity(typeList) {
    try {
      const params = { ...roomParams() }
      if (bucketS.value) params.bucket_s = bucketS.value
      if (typeList?.length) params.type = typeList.join(',')
      // 只统计**本次采集开始之后**的数据（需求）；浏览历史时再往左收窄到窗口起点，
      // 否则"我在看这一段"和"轴画的是全部历史"会互相矛盾。
      let since = startTs.value
      if (browsing.value && range.value?.lo) {
        since = Math.max(since, Number(range.value.lo))
      }
      if (since > 0) params.since = since
      const payload = await api.density(params)
      density.value = payload
      // 后端按跨度自适应时，把实际用的步长记回来（时间轴与"跳到某格"都要用它）
      if (!bucketS.value && payload?.bucket_s) bucketS.value = payload.bucket_s
    } catch (error) {
      fail(`加载时间轴失败：${error.message}`)
    }
  }

  /** 切换时间轴每格时长（用户选了具体值）。 */
  async function setBucketS(seconds) {
    const next = Number(seconds)
    if (!Number.isFinite(next) || next <= 0 || next === bucketS.value) return
    bucketS.value = next
    await loadDensity()
  }

  /** 按会话过滤（"只看某一次抓取"）。 */
  async function selectSession(id) {
    sessionId.value = id ?? null
    if (browsing.value) {
      focusTs.value = null
      await Promise.all([fetchPage({}, { replace: true }), loadDensity()])
    }
  }

  // ------------------------------------------------------------------ //
  // 标记（V3-a2）：存独立库，只依赖时间戳
  // ------------------------------------------------------------------ //

  /** 拉取当前房间的标记列表。库不存在时后端返回空列表，不是错误。 */
  async function loadBookmarks() {
    try {
      const payload = await api.annotations({ room_id: roomIdRef.value ?? undefined, limit: 500 })
      bookmarks.value = payload.items ?? []
      // 选中的标记若已被删除（或在别的房间），清掉选中态
      if (
        activeBookmarkId.value != null &&
        !bookmarks.value.some((item) => item.id === activeBookmarkId.value)
      ) {
        activeBookmarkId.value = null
      }
    } catch (error) {
      fail(`加载标记失败：${error.message}`)
    }
  }

  /**
   * 打一个标记。
   *
   * `recv_ts` 由调用方给（"此刻"或某条弹幕的时间）—— 这个函数不猜时间点：
   * 猜时间点会在"正在看历史"时标到实时那一刻，用户完全无从察觉。
   */
  async function addBookmark({ recvTs, label, note, anchorId, sessionId: sid } = {}) {
    const room = roomIdRef.value
    if (room == null) {
      fail('请先选择一个房间再打标记')
      return null
    }
    if (!Number.isFinite(recvTs)) {
      fail('无法确定标记的时间点')
      return null
    }
    try {
      const payload = await api.addAnnotation({
        room_id: room,
        recv_ts: recvTs,
        label: label || undefined,
        note: note || undefined,
        anchor_id: anchorId ?? undefined,
        session_id: sid ?? sessionId.value ?? undefined,
      })
      activeBookmarkId.value = payload.item?.id ?? null
      await loadBookmarks()
      return payload.item ?? null
    } catch (error) {
      fail(`打标记失败：${error.message}`)
      return null
    }
  }

  async function removeBookmark(id) {
    try {
      await api.removeAnnotation(id)
      if (activeBookmarkId.value === id) activeBookmarkId.value = null
      await loadBookmarks()
    } catch (error) {
      fail(`删除标记失败：${error.message}`)
    }
  }

  async function clearBookmarks() {
    // 逐个删（后端没提供批量删；本地用量下不构成问题，且避免"一键清空"误伤）
    const ids = bookmarks.value.map((item) => item.id)
    for (const id of ids) {
      try {
        await api.removeAnnotation(id)
      } catch (error) {
        fail(`删除标记失败：${error.message}`)
        break
      }
    }
    activeBookmarkId.value = null
    await loadBookmarks()
  }

  /** 当前选中的标记（导出与 UI 高亮用）。 */
  const activeBookmark = computed(
    () => bookmarks.value.find((item) => item.id === activeBookmarkId.value) ?? null,
  )

  /** 房间切换 / 原始数据被清空时调用：历史窗口整体失效。 */
  function reset() {
    historyItems.value = []
    pageHasOlder.value = false
    pageHasNewer.value = false
    detached.value = false
    exhausted.value = false
    focusTs.value = null
    activeBookmarkId.value = null
    requestSeq += 1
    loading.value = false
  }

  /** 组件卸载：阻断后续请求落到已销毁的组件上。 */
  function dispose() {
    disabled = true
  }

  /**
   * 合并视图：历史窗口 + 实时尾部，按 `(recv_ts, id)` 升序去重。
   *
   * 实时层始终参与合并 —— 即使在看历史，新弹幕也要能进得来（贴底时用户看到的是实时）。
   */
  const merged = computed(() => {
    const live = liveEvents.value
    const past = historyItems.value
    // 不在历史浏览中、或窗内没有历史时，直接返回实时（避免无谓的排序拷贝）
    if (!browsing.value || !past.length) return live

    const seen = new Set()
    const out = []
    let index = 0
    let liveIndex = 0
    // 两个有序数组归并（历史与实时各自都是 (recv_ts, id) 升序）
    while (index < past.length && liveIndex < live.length) {
      const left = past[index]
      const right = live[liveIndex]
      const takeLeft =
        left.recv_ts < right.recv_ts ||
        (left.recv_ts === right.recv_ts && left.id <= right.id)
      const item = takeLeft ? left : right
      if (takeLeft) index += 1
      else liveIndex += 1
      if (seen.has(item.id)) continue
      seen.add(item.id)
      out.push(item)
    }
    while (index < past.length) {
      const item = past[index++]
      if (!seen.has(item.id)) {
        seen.add(item.id)
        out.push(item)
      }
    }
    while (liveIndex < live.length) {
      const item = live[liveIndex++]
      if (!seen.has(item.id)) {
        seen.add(item.id)
        out.push(item)
      }
    }
    return out
  })

  return {
    browsing,
    loading,
    hasOlder,
    hasNewer,
    density,
    focusTs,
    windowS,
    sessionId,
    historyItems,
    range,
    merged,
    enter,
    exit,
    loadOlder,
    loadNewer,
    jumpTo,
    loadDensity,
    setBucketS,
    bucketS,
    startTs,
    setStartTs,
    selectSession,
    // 标记（V3-a2）
    bookmarks,
    activeBookmarkId,
    activeBookmark,
    loadBookmarks,
    addBookmark,
    removeBookmark,
    clearBookmarks,
    reset,
    dispose,
  }
}
