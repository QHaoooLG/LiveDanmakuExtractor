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
import {
  MIN_SPAN_S,
  clampSpan,
  reduceWindow,
  snapToBucket,
} from './useTimelineWindow.js'

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
  const density = shallowRef(null)
  /**
   * 服务端返回的**原始**密度响应（派生粒度用）。
   *
   * `density` 是"**显示用**"的那一份：它可能是把源数据按用户选的更粗档位**求和**得到的
   * 。所以两者必须分开 —— 源数据是事实，显示档位是选择。
   */
  const densitySource = shallowRef(null)
  /** 源数据归属的"数据身份"（房间/会话/类型/下界，**不含粒度**） */
  let densitySourceBase = null
  /** 当前**显示**档位（秒）：用户选的、或由源数据派生出来的 */
  const displayBucketS = ref(null)
  /**
   * **"我在看哪一段"的唯一真相源**（Phase 1）。
   *
   * `null` = 贴在实时边缘（没在看历史）。
   *
   * ⚠️ 高亮用的 `focusTs` 现在是**从它推导**的（只读 computed），不再是独立 ref。
   * 这是本阶段的核心：以前"我点了哪一段"由 `focusTs` + `range` + 滚动位置三者
   * 各自推导，任何一处漂移就是"高亮与点击不匹配"；现在只有一个锚点。
   * 规则与四条不变式见 `useTimelineWindow.js`。
   */
  const window = ref(null)
  /** 正在定位的时间点（**推导值**：`window.anchorTs`）—— 高亮只看它 */
  const focusTs = computed(() => window.value?.anchorTs ?? null)
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
  /**
   * 上一次**成功落地**的密度请求参数（`JSON.stringify`）。
   *
   * 用来跳过"输入完全没变"的重复请求 —— 点一格跳转会顺带调一次 `loadDensity()`，
   * 而那一次请求会把轴换掉、把滚动位置打回 0（详见 `loadDensity()` 的注释）。
   */
  let lastDensityKey = null
  /**
   * 最近一次**发出**的密度请求参数，用来丢弃过期的响应
   */
  let pendingDensityKey = null
  /**
   * 正在飞的那个密度请求的键（settle 后清空）。
   *
   * 用途：定时强制刷新（`force: true`）不该把自己叠起来 —— 上一次还没回来就再发
   * 一个，只会让 `density.value` 反复替换、白打请求。
   */
  let densityInFlight = null
  /**
   * 正在飞的那条请求的取消手柄（2026-10-09）。
   *
   * 键相同只说明"参数一样"，**挡不住参数不同的连点**：用户在 1/3/5 分钟档之间
   * 连点三下，就是三组不同的参数 ⇒ 三条请求同时飞。实测（`tools/probe_density_cost.py`）
   * 服务端每发 87 ms 里有 54 ms 是持 GIL 的 Python 折算，并发**超线性**变慢
   * （4 并发 912 ms、16 并发 6.0 s），约 40 并发就撞上 30 s 超时 ——
   * 这正是"连续点几下就报加载时间轴失败"的成因。
   *
   * 所以新请求发出前**先掐掉上一条**：服务端那边有协作式取消配合，
   * 客户端这边则保证"同一时刻最多一条密度请求在飞"。
   */
  let densityAbort = null
  /**
   * 正在飞的那条**历史分页**请求。
   *
   * 与 `densityAbort` 同一套做法：发新的一页之前先掐掉上一条。
   * 拖动时间轴时会连续发很多页，不掐就会堆并发（见 `fetchPage` 的说明）。
   */
  let historyAbort = null
  /** 待发的防抖定时器（连点时只让**最后一次**真正发出去） */
  let densityTimer = null
  /**
   * 等这次密度落地的人（防抖期间连点会攒下好几个）。
   *
   * `setBucketS()` 是 `async` 的，调用方（含用例）会 `await` 它 ——
   * 防抖把请求推迟了 250 ms，这些 await 必须**等到那次真正的加载完成**才 resolve，
   * 否则它们会在请求还没发出去时就返回（用例立刻断言 `calls` 就会扑空）。
   */
  let densityWaiters = []
  function flushDensityWaiters() {
    const waiters = densityWaiters
    densityWaiters = []
    for (const resolve of waiters) resolve()
  }
  /**
   * 连点粒度档位时的防抖窗口（毫秒）。
   *
   * 250 ms 是"手感"与"削减请求"的折中：人手连点两下的间隔通常 < 200 ms，
   * 拖过这个窗口就发；而真正想换档的人等 0.25 秒察觉不到（曲线随即重绘）。
   */
  const DENSITY_DEBOUNCE_MS = 250
  /**
   * **渲染预算**：这一屏最多画多少格。
   *
   * 由客户端算（它才知道视口多宽）：`曲线宽 / 每格最少像素`。
   * 经验值是"一格至少 ~2px 才看得出高低"，而时间轴宽度在常见窗口下约 900~1600px
   * （桌面全屏更宽）—— 取 1200px / 2px = 600 作缺省。
   * 服务端收到后会保证"手选档位 × 预算"不出格：超了就往上夹到档位阶梯上的下一档，
   * 并把 `bucket_s_clamped` 带回来（表头据此提示"已自动降档"）。
   */
  const DENSITY_MAX_SLOTS = 600
  /**
   * `bucketS` 是**用户手选的档位**（而不是后端自适应推出来的）。
   *
   * ⚠️ 2026-10-09 起，**自适应值不再写回 `bucketS`**（旧写法会把它"定终身"，
   * 见 `loadDensity()` 的说明）：`bucketS` 现在只表示"用户选了什么"，
   * 真正的粒度一律读响应里的 `density.bucket_s`。
   */
  let bucketExplicit = false

  function fail(error) {
    if (typeof onError === 'function') onError(error)
  }

  /**
   * 能不能从"更细的源数据"派生出"更粗的档位"。
   *
   * 计数是**完美可加**的，所以 60 秒的桶可以求出 300/600/900 秒的桶 ——
   * 只要"目标档位是源档位的整数倍"。更细或不对齐都不行（那需要重新取数）。
   */
  function canDerive(sourceStep, targetStep) {
    const source = Number(sourceStep)
    const target = Number(targetStep)
    if (!(source > 0) || !(target > 0)) return false
    if (target === source) return true
    return target > source && target % source === 0
  }

  /**
   * 把源密度**按更粗的档位求和**，得到"显示用"的那一份。
   *
   * 为什么这么做：切档原本每次都要发一次
   * `/api/density`（因为去重键里含 `bucket_s`），采集久了以后连点就是"卡滞"。
   * 而"粗档 = 细档求和"根本不需要服务端参与 —— 于是**往粗切档完全不用请求**。
   * 往细切档才需要请求（那次请求本身很小：行数一样，只是桶更细）。
   */
  function deriveDensity(payload, targetStep) {
    const target = Number(targetStep)
    if (!payload || !Array.isArray(payload.buckets) || !(target > 0)) return payload
    const source = Number(payload.bucket_s)
    if (!canDerive(source, target) || target === source) return payload
    const merged = new Map()
    for (const bucket of payload.buckets) {
      const key = Math.floor(Number(bucket.ts) / target) * target
      const count = Number(bucket.count) || 0
      const item = merged.get(key)
      if (item) item.count += count
      // 派生的桶里 `by_type` 一律 null：前端没有任何地方消费它
      //（`TypeBars` 用的是 `/api/stats.by_type`）
      else merged.set(key, { ts: key, count, by_type: null })
    }
    return {
      ...payload,
      bucket_s: target,
      buckets: [...merged.values()].sort((a, b) => a.ts - b.ts),
    }
  }

  /** 重新算一遍"显示用"的密度（源数据或显示档位变了就调它） */
  function refreshDisplayDensity() {
    const source = densitySource.value
    if (!source) {
      density.value = null
      return
    }
    const display = deriveDensity(source, displayBucketS.value ?? source.bucket_s)
    density.value = display
    // 密度落地 ⇒ 若当前"没在看历史"，把窗口放到数据末尾（让窗口带常驻）
    ensureLatestWindow()
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
      // **首屏**（无游标、非 replace）落在实时边缘：贴住新的那一端。
      //
      // ⚠️ `replace` 必须排除掉：`jumpTo()` 也是 replace，而它的语义是
      // "**我要看那一格**" —— 无条件把 `detached` 清成 false，就等于把跳转
      // 撤销了（`merged` 又去并入实时尾部，视口/数据都回到当下）。
      // 这正是"点历史时段却没跳过去"的直接原因：跳转确实发起了，
      // 但这一行在 `adopt()` 里立刻把它抹掉。
      if (!prepend && !page.has_newer && !replace) detached.value = false
    }
  }

  /**
   * 拉取一页。`cursor` 为空 = 首屏（最新一页）。
   *
   * ## 为什么要 `AbortController`
   *
   * `ticket !== requestSeq` 那个守卫只**丢弃过期结果** —— 请求本身照发照收：
   * 服务端要查一次库，浏览器要把一整页（`PAGE_SIZE=300`）JSON 解析出来再扔掉。
   * 而拖动时间轴时**每个 `pointermove` 都会跳转一次**（见 `jumpTo`），
   * 实测快速拖 2 秒能堆出上百个并发请求（`PAGE_SIZE` × 120 ≈ 3.6 万条事件对象）。
   *
   * 所以这里与 `loadDensity` 用同一套做法：发新请求前**掐掉上一条**。
   */
  async function fetchPage(cursor = {}, { replace = false, prepend = false } = {}) {
    if (disabled) return
    const ticket = ++requestSeq
    // 掐掉上一条：它的结果已经注定被 `ticket` 守卫丢弃，没必要让它跑完
    historyAbort?.abort()
    const controller = typeof AbortController === 'function' ? new AbortController() : null
    historyAbort = controller
    loading.value = true
    try {
      const page = await api.history(
        { ...roomParams(), limit: PAGE_SIZE, ...cursor },
        { signal: controller?.signal },
      )
      if (ticket !== requestSeq) return // 已有更新的请求，丢弃这一份
      adopt(page, { replace, prepend })
    } catch (error) {
      // 被我们自己取消的那条**不是错误**（拖动时必然发生），别弹横幅
      if (error?.name === 'AbortError') return
      fail(`加载历史失败：${error.message}`)
    } finally {
      if (historyAbort === controller) historyAbort = null
      if (ticket === requestSeq) loading.value = false
    }
  }

  /** 进入历史浏览：从最新一页开始。 */
  async function enter() {
    if (browsing.value) return
    browsing.value = true
    // 未指定锚点 ⇒ 贴在实时边缘（`focusTs` 由窗口推导，自然是 null）
    window.value = null
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
    window.value = null // 锚点跟着清掉（`focusTs` 推导出来也就是 null）
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
   * 时间轴当前的**格子网格**（`/api/density` 回来的 `bucket_s`）。
   *
   * ⚠️ 只需要 `bucket_s`，**不需要** `range.lo`：网格是 **epoch 对齐**的
   * （后端 `store.density()` 里写的是 `key = int(recv_ts / bucket_s)`），
   * 而 `range.lo` 是"范围内第一条数据的时间"（实测带小数、还会随查询变），
   * 拿它当原点会系统性偏格 —— 实测踩到过（详见 `useTimelineWindow.js` 的说明）。
   *
   * 拿它把锚点**吸附到格中心**，这样"高亮哪一格"可以由锚点纯算出来（不变式 3）。
   * 还没拿到密度时返回 `null`。
   */
  function densityGrid() {
    const span = Number(density.value?.bucket_s)
    if (!Number.isFinite(span) || span <= 0) return null
    return { bucketS: span }
  }

  /** 数据的真实范围（用于把窗口夹在数据内）；拿不到就不夹。 */
  function dataBounds() {
    const range = density.value?.range
    const lo = Number(range?.lo)
    const hi = Number(range?.hi)
    return Number.isFinite(lo) && Number.isFinite(hi) ? { lo, hi } : {}
  }

  /**
   * 跳到某个时间点（点时间轴的某一格）。
   *
   * ## 锚点 = 唯一真相（Phase 1）
   *
   * 历史上这里是"直接写 `focusTs`、再自己算一个 `until`"：
   *
   * ```js
   * focusTs.value = ts
   * const bucketEnd = ts + bucketS          // ← 第二处独立推导
   * fetchPage({ until: bucketEnd, ... })    // ← 第三处独立推导
   * ```
   *
   * 三处各自算一次，任何一处与画出来的格子不对齐，用户看到的就是
   * "高亮时段与点击时段不匹配"。现在全部收敛成**一个 action**：
   *
   * ```js
   * window.value = reduceWindow(window.value, { type: 'point', ts, spanS }, bounds)
   * ```
   *
   * 于是：高亮 = `window.anchorTs`（`focusTs` 由它推导）、
   * 请求右端 = `window.hi`。**三者不可能再各自漂移。**
   *
   * ## 吸附到格中心（`snapToBucket`）
   *
   * 锚点先吸附到所点那一格的**中心**，于是窗口
   * `[center - span/2, center + span/2]` 恰好与该格对齐 —— `window.hi`
   * 自然等于**格尾**（这正是 2026-10-05 那次 off-by-one 的修法所要的语义，
   * 现在它是"吸附"的推论，不再是一条需要有人记得维护的约定）。
   *
   * 锚点落在所点格子内 ⇒ 高亮必然就是那一格；吸附到中心还额外保证它离
   * 格子边界有半格余量，浮点尾差不会把它算到邻格。
   */
  async function jumpTo(ts, bucketS = 60) {
    const grid = densityGrid()
    const span = clampSpan(Number(bucketS) || grid?.bucketS || 60)
    // 能吸附就吸附（落在格中心）；没有网格可吸附时保持原值（例如密度还没回来）
    const anchorTs = snapToBucket(ts, grid ?? {}) ?? Number(ts)
    if (!Number.isFinite(anchorTs)) return

    window.value = reduceWindow(
      window.value,
      { type: 'point', ts: anchorTs, spanS: span },
      dataBounds(),
    )
    browsing.value = true
    detached.value = true
    exhausted.value = false
    liveLimit.value = LIVE_TAIL_WHEN_HISTORY

    // 请求右端**由窗口推导**（不再是"锚点 + bucketS"这种第二处算术）
    const until = window.value?.hi ?? anchorTs + span
    await Promise.all([
      fetchPage(
        { until, before_ts: until, before_id: Number.MAX_SAFE_INTEGER },
        { replace: true },
      ),
      loadDensity(),
    ])
  }

  /**
   * 时间轴密度（按当前房间/会话/类型过滤 + 用户选的格宽）。
   *
   * ## 为什么同样的输入**不再重复请求**（`lastDensityKey`）
   *
   * 时间轴是"本次采集全程的一根尺"，它的输入只有四样：房间、会话、类型、格宽。
   * 而点一格跳转会调 `loadDensity()` —— 那时这四样**一个都没变**，重拉纯属浪费，
   * 而且**有害**：请求是异步的，重拉期间 `density.value` 会被换掉，
   * 内容宽跟着变（`scrollWidth` 缩到视口宽再涨回来），浏览器就把滚动容器的
   * `scrollLeft` 强制归 0 —— 用户看到的是"点了一下，高亮的那一格被甩出屏幕"。
   * 实测（真 Chrome，`tools/probe_timeline_density_calls.py`）：
   * 点完约 1.5 s `scrollWidth` 从 2250 掉到 394、`scrollLeft` 归 0。
   *
   * 这个"轴会缩"的现象与早先那个 `since = max(startTs, range.lo)` 是**同一类病**
   * （轴跟着浏览窗口变），所以修法也一致：**轴的输入不变就不动它**。
   *
   * ## `force`：定时刷新要能突破去重
   *
   * 去重键里**不含时间**，所以采集在继续、键却不变 —— 定时刷新如果走同一条路
   * 会被自己挡掉，曲线于是**永远停在打开面板那一刻**（需求方实测："曲线不会根据
   * 时间更新"）。所以定时刷新用 `force: true` 显式请求。
   * 同参数的请求正在飞时仍然跳过（`densityInFlight`），不叠加。
   *
   * ## ⚠️ 只有用户手选的档位才带 `bucket_s`（2026-10-09，实测 8.85 天轴的元凶之一）
   *
   * 旧写法是"把后端自适应推出来的步长记回 `bucketS`，之后每次都带着它请求"。
   * 两个后果：
   *   1. 首次请求的口径可能是"全部房间"（还没点选房间），于是**按整库跨度**挑出来的
   *      步长（实测 7200 秒）被固化下来，点选房间后依旧按 2 小时/格画 ——
   *      实测轴长 8.85 天、曲线只有 3 个点；
   *   2. 采集在增长、而粒度**永不重算**（"抓一次就定终身"）。
   * 现在：`bucket_s` 只来自**用户的选择**；自适应值每次由后端重算，
   * 真正的粒度一律读响应里的 `density.bucket_s`。
   */
  async function loadDensity(typeList, { force = false } = {}) {
    // 立即加载（首屏/换房间/定时刷新）之前，把排队中的防抖请求作废 ——
    // 否则"点了档位、紧接着切房间"会先发一趟旧房间的密度。
    if (densityTimer !== null) {
      clearTimeout(densityTimer)
      densityTimer = null
    }
    const params = { ...roomParams() }
    if (bucketExplicit && bucketS.value) params.bucket_s = bucketS.value
    if (typeList?.length) params.type = typeList.join(',')
    // **渲染预算**：按视口宽算"最多能画多少格"，让服务端在手选档位
    // 超出预算时自动夹粗（并把实际档位回给我们）。1200px 视口 / 2px 一格 ≈ 600。
    params.max_slots = DENSITY_MAX_SLOTS
    // ⚠️ **只准有一个下界：本次采集开始时刻**（`startTs`）。
    //
    // 曾经这里还有一句 `since = Math.max(since, range.lo)`（浏览历史时把轴收窄到
    // 当前窗口起点）—— 那是**错的**，而且错得很隐蔽：点任意一格都会触发
    // `loadDensity()`，于是"点一下历史时段，整条时间轴就缩成那 300 条的范围"，
    // 完全违背"轴画本次采集全程"的设计初衷（需求方实测反馈）。
    //
    // 当初加它的理由其实是"让高亮不落到轴外" —— 那是**高亮**的事：
    // 窗口区间通过 `range` 传给渲染层就够了，不需要改写轴的数据范围。
    if (startTs.value > 0) params.since = startTs.value

    // 数据身份（**不含粒度**：`bucket_s` 必须剔除，否则"同一段数据的两个档位"
    // 会被当成两份数据，派生捷径永远不命中 —— 第一版就是这么错的）。
    const baseKey = JSON.stringify({ ...params, bucket_s: undefined })
    const wanted = bucketExplicit && bucketS.value ? Number(bucketS.value) : null

    // **往粗切档不必发请求** —— 直接把已有的细数据求和。
    // 注意 `force`（6 秒定时刷新）必须真的重取，所以它绕过这条捷径。
    if (!force && densitySourceBase === baseKey && wanted && displayBucketS.value !== wanted
        && canDerive(densitySource.value?.bucket_s, wanted)) {
      displayBucketS.value = wanted
      refreshDisplayDensity()
      flushDensityWaiters()
      return
    }

    const key = JSON.stringify(params)
    // ⚠️ 这两条早退**必须**先放行攒着的等待者（`flushDensityWaiters`）。
    // 它们跳过了下面的 `try/finally`，而等待者是靠 `finally` 里的 flush 释放的 ——
    // 少了这一句，`queueDensity()` 推入的 resolver 会一直挂在
    // `densityWaiters` 里（连带闭包与 `await setBucketS()` 的调用方一起泄漏），
    // 直到某次**成功的**加载才被顺手清掉。相邻的"派生捷径"分支
    // （上面那段）就是显式 flush 的 —— 意图本来是一致的。
    if (!force && key === lastDensityKey) {
      flushDensityWaiters()
      return
    }
    // 同参数已经在飞：跳过（定时刷新不该把自己叠起来）
    if (key === densityInFlight) {
      flushDensityWaiters()
      return
    }
    // 参数不同也不行：**掐掉上一条**再发（连点档位的雪崩源头就在这里）
    densityAbort?.abort()
    const controller = typeof AbortController === 'function' ? new AbortController() : null
    densityAbort = controller
    densityInFlight = key
    pendingDensityKey = key
    try {
      const payload = await api.density(params, { signal: controller?.signal })
      // 只有**最新那次请求**才落地：期间输入又变了的话，这一份已经过期
      if (key !== pendingDensityKey) return
      lastDensityKey = key
      densitySource.value = payload
      densitySourceBase = baseKey
      // 显示档位：用户选的档位若能被响应整除（且**不比响应更细**）就用它，
      // 否则用响应里的实际档位 —— 服务端可能因为渲染预算夹粗了它，
      // 那时"显示 1 分钟、数据其实是 15 分钟"就是撒谎。
      const actual = Number(payload?.bucket_s)
      displayBucketS.value = wanted && canDerive(actual, wanted) ? wanted : actual
      refreshDisplayDensity()
    } catch (error) {
      // 被我们自己取消的那条（切了档位/换了房间）**不是错误**，别弹横幅
      if (error?.name === 'AbortError') return
      fail(`加载时间轴失败：${error.message}`)
    } finally {
      if (densityInFlight === key) densityInFlight = null
      if (densityAbort === controller) densityAbort = null
      // 这一次（无论是立即发的还是防抖后发的）已经有结果了：
      // 攒着的 `await setBucketS()` 到这里统一放行
      flushDensityWaiters()
    }
  }

  /**
   * **连点粒度档位走这条**：防抖 + 只发最后一次。
   *
   * 为什么不能直接 `loadDensity()`：切档是**连续动作**（用户在 1/3/5/10 分钟之间
   * 反复比划），每次参数都不同 ⇒ 去重键各不同 ⇒ 全部同时飞。
   * 实测这条链路的雪崩过程：单发 87 ms → 4 并发 912 ms → 16 并发 6.0 s，
   * 约 40 并发就是客户端那条 30 s 超时。
   *
   * 所以：**250 ms 内的连点只保留最后一次**，其余直接作废（连请求都不发）。
   */
  function queueDensity(typeList) {
    if (densityTimer !== null) clearTimeout(densityTimer)
    const settled = new Promise((resolve) => densityWaiters.push(resolve))
    densityTimer = setTimeout(() => {
      densityTimer = null
      // `loadDensity()` 的 finally 会 flush 所有等待者（见上面的说明）
      loadDensity(typeList)
    }, DENSITY_DEBOUNCE_MS)
    return settled
  }

  /** 切换时间轴每格时长（用户选了具体值）。
   *
   *  走 `queueDensity()`（防抖 + 只发最后一次）而不是直接 `loadDensity()`：
   *  用户在档位之间连点是最常见的操作，也是"切粒度卡滞"的源头。
   */
  async function setBucketS(seconds) {
    const next = Number(seconds)
    if (!Number.isFinite(next) || next <= 0 || next === bucketS.value) return
    bucketS.value = next
    // 用户选了档位 ⇒ 之后它必须参与去重键、并且真的要带 `bucket_s` 去请求
    bucketExplicit = true
    // ⚠️ 必须 `return`：调用方（含用例）会 `await setBucketS()` 等那次加载落地
    return queueDensity()
  }

  /**
   * 把窗口**初始化到数据末尾的一段**（需求方 2026-10-09 拍板）。
   *
   * ## 为什么要有它
   *
   * 窗口带（时间轴上那段高亮区间）以前只在"点过时间轴"之后才存在 ——
   * 于是刚打开面板时既没有播放头也没有带子，"我在看哪一段"这件事**在界面上不存在**，
   * 用户必须先点一下才能拖它。现在把它变成**常驻**：只要还没在看历史，
   * 窗口就默认停在**最新数据**上（`latest` action，锚点 = 数据末尾）。
   *
   * ## 时机与边界
   *
   * * 每次密度落地后调用一次，但**只在 `window` 为空时**才初始化 ——
   *   用户点过/拖过之后绝不能被它覆盖；
   * * 段长用**用户手选的粒度**（`bucketS`），没选过就用 `MIN_SPAN_S`（1 分钟）；
   * * 只在**没有在浏览历史**时做：`exit()`（回到实时）之后窗口会被清掉，
   *   下一轮密度刷新会把它重新放回末尾 —— 这正是"带子常驻"的语义；
   * * 数据范围比一段还窄时由 `normalize()` 兜底（贴住数据起点、锚点夹进窗口）。
   */
  function ensureLatestWindow() {
    if (browsing.value) return
    if (window.value) return
    const hi = Number(density.value?.range?.hi)
    if (!Number.isFinite(hi)) return
    const span = clampSpan(bucketS.value ?? MIN_SPAN_S)
    window.value = reduceWindow(null, { type: 'latest', hi, spanS: span }, dataBounds())
  }

  /**
   * 只改**在看多长的一段**（窗口段长），不动密度粒度、也不动弹幕区。
   *
   * ⚠️ **当前没有 UI 控件调用它**（2026-10-09 起）：曲线视图的「每格 X 分钟」
   * 已改为控制**采样粒度**（`setBucketS`），段长则由曲线的手势经 `jump`
   * （载荷 `{ ts, spanS }`）交给 `jumpTo()` —— 那条路径同时会定位弹幕区（这是
   * 想要的：拖窗口端点就是在"看那一段"）。这里保留它作为"只改段长、不跳转"的
   * API（reducer 的 `resize` 动作），供将来单独放一个"视野宽度"控件时用。
   */
  function setSpanS(seconds) {
    const next = clampSpan(Number(seconds))
    if (!window.value) {
      // 还没进入历史浏览：先以数据末尾为锚点开一个窗口（用户拖了窗口带就该进历史）
      const hi = Number(density.value?.range?.hi)
      if (!Number.isFinite(hi)) return
      window.value = reduceWindow(null, { type: 'latest', hi, spanS: next }, dataBounds())
      return
    }
    window.value = reduceWindow(window.value, { type: 'resize', spanS: next }, dataBounds())
  }

  /** 按会话过滤（"只看某一次抓取"）。 */
  async function selectSession(id) {
    sessionId.value = id ?? null
    if (browsing.value) {
      window.value = null // 换会话 ⇒ 锚点失效
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
    window.value = null // 锚点跟着清掉
    activeBookmarkId.value = null
    requestSeq += 1
    loading.value = false
    // 房间换了 ⇒ 上次那份密度不再对应当前输入，必须允许重新拉；
    // 用户手选的档位也跟着作废（换房间后按新跨度重新自适应更合理）
    lastDensityKey = null
    pendingDensityKey = null
    densityInFlight = null
    // 排队中/在飞的那条也要掐掉：否则它会带着**上一个房间**的参数落地
    if (densityTimer !== null) {
      clearTimeout(densityTimer)
      densityTimer = null
    }
    densityAbort?.abort()
    densityAbort = null
    // 历史分页同理：在飞的那页带着上一个房间的参数，别让它落地
    historyAbort?.abort()
    historyAbort = null
    // 源数据与显示档位一起作废：换房间后"手上那份密度"不再对应当前输入
    densitySource.value = null
    densitySourceBase = null
    displayBucketS.value = null
    density.value = null
    bucketS.value = null
    bucketExplicit = false
  }

  /**
   * 组件卸载：阻断后续请求落到已销毁的组件上。
   *
   * 不能只置 `disabled = true`：那样**待触发的防抖定时器**仍会跑一次
   * `loadDensity()`，**在飞的请求**也会照常回来写 ref（`densitySource.value = …`）
   * —— 组件已经销毁，那些写入是纯浪费，还可能触发已卸载的渲染。
   * 所以这里把定时器与两个在飞请求都收干净（`reset()` 里是同一套动作）。
   */
  function dispose() {
    disabled = true
    if (densityTimer !== null) {
      clearTimeout(densityTimer)
      densityTimer = null
    }
    densityAbort?.abort()
    densityAbort = null
    historyAbort?.abort()
    historyAbort = null
    // 攒着的 `await setBucketS()` 也要放行，否则调用方会一直挂着
    flushDensityWaiters()
  }

  /**
   * 合并视图。
   *
   * ## `detached` 时**只给历史窗口**（这是一个真 bug 的修法）
   *
   * 原来无条件把实时尾部并进来，于是"跳过去看历史"从根上做不到：
   * 合并后最新那批**永远是实时弹幕**，而 `StreamList` 只渲染**最后 400 条** ——
   * 用户点历史时段，视口却停在当下（需求方实测反馈"没跳过去 / 弹幕区没变化"）。
   *
   * 去掉实时尾部才是对的（不是"顺手砍功能"）：
   *   * `detached` 的含义本来就是"**窗口已离开实时边缘**"（`loadOlder` 与 `jumpTo`
   *     都会置位），此时用户要看的就是那一段；
   *   * 实时层**照旧在累积**（`liveEvents` 一直在涨），点「回到实时」立刻恢复全量
   *     —— 不丢数据，只是不混着显示；
   *   * 与既有 `hasNewer` 状态机一致：`detached` 就是 `hasNewer === true`。
   *
   * 贴住实时边缘时（首屏进入历史浏览）仍然合并：那时实时尾部与历史窗口在时间上
   * 连续，不合并反而会把刚进来的新弹幕丢掉。
   */
  const merged = computed(() => {
    const past = historyItems.value
    if (!browsing.value || !past.length) return liveEvents.value
    if (detached.value) return past
    return mergeSorted(past, liveEvents.value)
  })

  /** 两个有序数组按 `(recv_ts, id)` 归并去重（历史与实时各自升序）。 */
  function mergeSorted(past, live) {
    const seen = new Set()
    const out = []
    let index = 0
    let liveIndex = 0
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
  }

  return {
    browsing,
    loading,
    hasOlder,
    hasNewer,
    /**
     * 窗口是否**已经离开实时边缘**（向上翻过页，或时间轴定位过）。
     *
     * 暴露出去有两个用处：`merged` 靠它决定"要不要把实时尾部并进来"
     * （离开边缘时**不并**，否则跳转会被实时弹幕顶走），
     * UI 也可以据此显示"回到了哪一段"。
     */
    detached,
    density,
    /**
     * **"我在看哪一段"的唯一真相源**（Phase 1）：`{ lo, hi, spanS, anchorTs }` 或 `null`。
     *
     * `focusTs` 是它的推导值；时间轴要显示"高亮哪一格"只该读 `focusTs`，
     * 想让时间轴**改变**在看哪一段则调 `jumpTo()`（它内部走 reducer），
     * 不要从外面直接改窗口 —— 那会让"唯一真相源"退化成一个新的中间层。
     */
    window,
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
    setSpanS,
    /**
     * 把窗口放到"数据末尾的一段"（窗口带常驻用）。
     *
     * `loadDensity()` 已经会调它；对外暴露是为了让"清空窗口"之后的调用方
     * （例如 `exit()` / `reset()` 的收尾）能立刻放回去，而不必等下一轮刷新。
     */
    ensureLatestWindow,
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
