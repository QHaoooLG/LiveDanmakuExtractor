/**
 * 时间轴窗口 —— **"我在看哪一段"的唯一真相源**（Phase 1）。
 *
 * ## 这个模块解决的是什么
 *
 * 时间轴历史上反复出"高亮时段与点击时段不匹配"，根因不是某一处算错，而是
 * **"我点了哪一段"这件事没有单一真相源**：高亮位置由"滚动位置 + 历史窗口 +
 * `focusTs`"三个变量各自推导，三者任何一处漂移，用户看到的就是错位。
 *
 * 这个模块把它收敛成一个纯函数 reducer：
 *
 * ```
 * window = { lo, hi, spanS, anchorTs }
 * reduceWindow(window, action) -> window
 * ```
 *
 * 一次交互（点击/拖动/键盘/换粒度）**只产生一个 action**，其余全部**推导**：
 *
 * | 要什么 | 怎么来 |
 * |--------|--------|
 * | 请求哪一段（`/api/history` 的锚点） | `window.anchorTs` + `window.spanS` |
 * | 高亮哪一格 | `highlightIndexOf(window, bars)` —— **只读 `anchorTs`** |
 * | 主区滚到哪一条 | `anchorTs` 前后的那一段 |
 * | 时间轴要不要加下界 | 与窗口无关（只看"本次采集起点"，见 `axisAnchor.js`） |
 *
 * ## 四条不变式（就是本模块的测试清单）
 *
 * 1. `lo <= anchorTs <= hi`，且 `hi - lo === spanS`；
 * 2. **高亮下标只由 `anchorTs` 决定**（`highlightIndexOf`），不由滚动位置、不由窗口推导；
 * 3. **高亮下标 == `truncate((anchorTs - range.lo) / bucket_s)`**（用 `/api/density`
 *    回来的桶起点算）—— 这是"点哪格亮哪格"的形式化写法；
 * 4. 同一个 `window` 与同一份 `bars` 求值两次得到同一个结果（纯函数）。
 *
 * ## 与旧实现的关系
 *
 * 旧的 `timeline.focusIndexOf()` 用"区间判定 + 最近中心点兜底"来猜"点的是哪一格"，
 * 而这里**不再猜**：`anchorTs` 就是用户点的那一刻，高亮下标由它直接算出。
 * 兜底逻辑（`anchorTs` 落在没有数据的空档里）改由 `nearestBarIndex()` 明确表达，
 * 且只在"画图找位置"时用，不影响"高亮哪一格"。
 */

import { BUCKET_CHOICES } from '../timeline.js'

/** 每段最短/最长（秒）：与时间轴的可选档位一致（最短 1 分钟，最长 15 分钟）。 */
export const MIN_SPAN_S = Math.min(...BUCKET_CHOICES.map((item) => item.value))
export const MAX_SPAN_S = Math.max(...BUCKET_CHOICES.map((item) => item.value))

/** 把任意输入夹成"合法的一格秒数"。 */
export function clampSpan(seconds) {
  const value = Number(seconds)
  if (!Number.isFinite(value) || value <= 0) return MIN_SPAN_S
  return Math.min(Math.max(value, MIN_SPAN_S), MAX_SPAN_S)
}

/**
 * 由"锚点 + 段长"造一个窗口。
 *
 * 锚点取窗口**中心**：这样"我想看 08:15 前后"在两边都不会因为贴到数据边缘而失衡；
 * 用户拖窗口两端把手时只改 `spanS`，中心不动 —— 手感与常见的时间轴一致。
 *
 * ⚠️ **`bounds` 是第二个参数，别写进第一个对象里**（2026-10-09 实测）：
 * 这里曾经写成 `createWindow({ anchorTs, spanS, lo, hi } = {})` —— 于是
 * `reduceWindow()` 传进来的 `bounds`（第二个实参）被**静默丢掉**，
 * 所有由 reducer 产生的窗口**从来就没被夹过**。现象（真机实测）：
 * 点时间轴最左端，窗口左端探出数据 30 秒（`data-selection` 读出
 * `[23:19:55, 23:20:55]` 而数据从 `23:20:25` 才开始），窗口带被画成 35 秒宽
 * （本该 60 秒）。
 */
export function createWindow({ anchorTs, spanS = MIN_SPAN_S } = {}, bounds = {}) {
  const span = clampSpan(spanS)
  const anchor = Number(anchorTs)
  if (!Number.isFinite(anchor)) return null
  const half = span / 2
  return normalize({ lo: anchor - half, hi: anchor + half, spanS: span, anchorTs: anchor }, bounds)
}

/**
 * 夹住窗口并**保证不变式 1**（`lo <= anchorTs <= hi`、`hi - lo === spanS`）。
 *
 * `bounds` 可选：给了就把窗口限制在数据范围内（`[bounds.lo, bounds.hi]`）。
 *
 * ## 2026-10-09 修正：**部分越界也要处理**（整体平移，锚点不动）
 *
 * 旧实现只在"整段都在数据外"时才夹（`hi <= boundLo` / `lo >= boundHi`），
 * 于是"窗口有一半探出数据"这种情况被放过去了。后果是真机实测到的：
 *
 * * 窗口探到数据左边 ⇒ 时间轴的定义域跟着被撑大 ⇒ **左边凭空多出一段幻影基线**；
 * * 用户点轴的左端/中间时，`hi <= boundLo` 那条分支把锚点**夹到数据起点** ——
 *   点 x=197（中间）却把播放头画到 x=377（最右），交互完全反常态。
 *
 * 现在的规则：**窗口装得下就整体平移进数据范围**（`lo/hi` 一起挪，锚点保持不动 ——
 * 用户点的是哪一刻就还是哪一刻）；只有"数据范围本身比一段还窄"时才退回贴边 +
 * 夹锚点（那种情况下不变式 1 优先）。
 */
export function normalize(window, bounds = {}) {
  if (!window) return null
  const span = clampSpan(window.spanS)
  let anchor = Number(window.anchorTs)
  if (!Number.isFinite(anchor)) return null
  let lo = anchor - span / 2
  let hi = lo + span

  const boundLo = Number(bounds?.lo)
  const boundHi = Number(bounds?.hi)
  const bounded = Number.isFinite(boundLo) && Number.isFinite(boundHi) && boundHi > boundLo
  if (bounded) {
    if (span <= boundHi - boundLo) {
      // 装得下 ⇒ 整体平移（锚点在窗口里的相对位置**不变**）。
      // 两端都**从边界重新算一遍**（而不是 `lo -= hi - boundHi`）：大时间戳上做加减
      // 会带来浮点尾差，`hi - lo === spanS` 这条不变式会以 299.9996 这种形式被破坏。
      if (lo < boundLo) {
        lo = boundLo
        hi = lo + span
      } else if (hi > boundHi) {
        hi = boundHi
        lo = hi - span
      }
    } else {
      // 数据范围比一段还窄：贴住左端，锚点夹进窗口（必要时会动锚点）
      lo = boundLo
      hi = lo + span
    }
    anchor = Math.min(Math.max(anchor, lo), hi)
  }
  return { lo, hi, spanS: span, anchorTs: anchor }
}

/**
 * reducer：**唯一**改变"在看哪一段"的入口。
 *
 * | action | 含义 | 谁会发 |
 * |--------|------|--------|
 * | `point`  | 定位到某一时刻（单击/拖动播放头/键盘平移） | 时间轴点格、方向键 |
 * | `resize` | 改段长（拖窗口两端把手、换粒度档位） | 把手、档位按钮 |
 * | `move`   | 平移窗口但不换锚点（拖窗口中间） | 拖窗口带 |
 * | `zoom`   | 以锚点为中心缩放（滚轮） | 滚轮 |
 * | `latest` | 回到数据末尾并退出历史 | 「回到实时」 |
 * | `clear`  | 清掉窗口（回实时） | 退出历史浏览 |
 *
 * ⚠️ **一次手势只发一个 action**（Phase 1 的核心约束）。所以"选一个区间"
 * 不能用 `point` + `resize` 两步表达 —— 那中间会经过一个**段长还是旧值**的瞬间，
 * 扇出去的 `until` 就是错的。`point` 因此允许**自带 `spanS`**：给了就一起换。
 *
 * `bounds` 是数据的真实范围（来自 `/api/density` 的 `range`）；没给就不夹。
 */
export function reduceWindow(window, action, bounds = {}) {
  const type = action?.type
  if (type === 'clear') return null

  if (type === 'latest') {
    const span = clampSpan(action.spanS ?? window?.spanS ?? MIN_SPAN_S)
    const hi = Number(action.hi)
    if (!Number.isFinite(hi)) return window ?? null
    // ⚠️ **必须把 `bounds` 传下去**（2026-10-09 实测）：`normalize()` 会用
    // `anchorTs` 重新推导 `lo/hi`（它按"锚点在正中"的不变式工作），所以
    // "右端贴住数据末尾"这个意图只能靠 **bounds 的平移**来实现 —— 不传的话
    // 窗口会以数据末尾为**中心**，右半段伸出数据之外（实测：数据到 23:20:19，
    // 窗口右端跑到 23:20:49）。
    return normalize({ lo: hi - span, hi, spanS: span, anchorTs: hi }, bounds)
  }

  if (type === 'point') {
    const anchorTs = Number(action.ts)
    if (!Number.isFinite(anchorTs)) return window ?? null
    // `spanS` 是**可选**的：曲线视图点一下要同时换锚点与段长（一个 action 搞定）；
    // 不带就沿用当前段长（时间轴点格就是这种）。
    const rawSpan = action.spanS ?? window?.spanS
    return createWindow({ anchorTs, spanS: rawSpan }, bounds)
  }

  if (type === 'resize') {
    if (!window) return null
    return createWindow({ anchorTs: window.anchorTs, spanS: action.spanS ?? action.span }, bounds)
  }

  if (type === 'move') {
    if (!window) return null
    const delta = Number(action.deltaTs)
    if (!Number.isFinite(delta)) return window
    return createWindow({ anchorTs: window.anchorTs + delta, spanS: window.spanS }, bounds)
  }

  if (type === 'zoom') {
    if (!window) return null
    const next = clampSpan(action.spanS ?? window.spanS * (action.factor ?? 1))
    return createWindow({ anchorTs: window.anchorTs, spanS: next }, bounds)
  }

  // 认不出的 action 不改状态（宁可不动，也不要猜）
  return window ?? null
}

/**
 * **不变式 2/3**：高亮的是第几格 —— 只读 `anchorTs`。
 *
 * 下标按**epoch 网格**算（与后端 `int(ts/bucket_s)` 同源，见上面的说明），
 * 再减去"第一根柱子的下标"换算成**柱数组下标**。
 *
 * 为什么要减：`/api/density` 只返回**有数据**的桶，柱数组的下标 0 对应用户看到的
 * 第 0 格，而它在 epoch 网格里的下标是 `floor(firstBarTs / bucket_s)`。两者之差
 * 就是那个偏移。**不要**改用 `range.lo` 当原点 —— 那是数据起点、不是桶边界。
 *
 * 返回 `-1` 表示"没有可高亮的格"（还没拿到密度 / 锚点在数据范围之外）。
 */
export function highlightIndexOf(window, { bars, bucketS } = {}) {
  if (!window) return -1
  const anchor = toFiniteOrNull(window.anchorTs)
  const span = toFiniteOrNull(bucketS)
  const list = bars ?? []
  if (anchor == null || span == null || span <= 0 || !list.length) return -1
  const firstTs = toFiniteOrNull(list[0]?.ts)
  if (firstTs == null) return -1
  const base = Math.floor(firstTs / span)
  const index = Math.floor(anchor / span) - base
  if (index < 0 || index >= list.length) return -1
  return index
}

/**
 * 画图用：`anchorTs` 落在哪根柱子上（**不参与"高亮哪一格"的判定**）。
 *
 * 与 `highlightIndexOf` 分开是刻意的：高亮必须是"索引 = 算出来的格"，
 * 而"锚点落在空档里时把播放头画在最近的一格上"只是**视觉落点**。
 * 把这两件事混在一起，就是旧实现 `focusIndexOf()` 那种"又判定又兜底"的糊法。
 */
export function nearestBarIndex(bars, ts) {
  const list = bars ?? []
  if (!list.length || ts == null) return -1
  const target = Number(ts)
  if (!Number.isFinite(target)) return -1
  let best = 0
  let bestDelta = Infinity
  list.forEach((bar, index) => {
    const delta = Math.abs(Number(bar.ts) - target)
    if (delta < bestDelta) {
      best = index
      bestDelta = delta
    }
  })
  return best
}

/**
 * 把"可能是 null / undefined / 空串"的输入转成数字；拿不到有效数字时返回 `null`。
 *
 * ⚠️ 不能直接 `Number.isFinite(Number(x))`：`Number(null)` 与 `Number('')` 都是 **0**，
 * 而 0 是有限数 —— 于是"没有下界"会被当成"下界 = 0"（实测踩到：
 * `snapToBucket(ts, { bucketS: null })` 返回了一个看起来正常的时刻，而不是 `null`）。
 * 判"有没有值"和判"是不是数字"必须分开做。
 */
function toFiniteOrNull(value) {
  if (value === null || value === undefined || value === '') return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

/**
 * ⚠️ **时间轴的网格是"epoch 对齐"的，不是"range.lo 对齐"的。**
 *
 * 后端的桶边界写的是（`store.density()`）：
 *
 * ```python
 * key = int(recv_ts / bucket_s)      # ← 除以 bucket_s，**从 0（epoch）起算**
 * ts  = key * bucket_s
 * ```
 *
 * 而 `range.lo` 是"该范围内**第一条数据**的时间"（实测带小数：`1791429480.017`），
 * **不是**桶起点。所以：
 *
 * * 第 k 格的起点 = `k * bucket_s`；
 * * 第 k 格 = `[k*bucket_s, (k+1)*bucket_s)`；
 * * 下标 = `floor(ts / bucket_s)`。
 *
 * 早先这里写成 `floor((ts - rangeLo) / bucketS)`（把 `range.lo` 当网格原点）——
 * 那会**系统性地偏若干格**（两个来源的 `range.lo` 差几十秒就偏一格），
 * 表现就是"高亮时段与点击时段不匹配"。真机实测踩到过：同一个锚点，
 * 用 `range.lo` 算得第 88 格、用 epoch 算得第 89 格，而画出来的是第 89 格。
 */

/** 用**epoch 网格**算某个时刻在第几格（与后端 `int(ts/bucket_s)` 同一套）。 */
export function gridIndexOf(ts, { bucketS } = {}) {
  const value = toFiniteOrNull(ts)
  const span = toFiniteOrNull(bucketS)
  if (value == null || span == null || span <= 0) return -1
  return Math.floor(value / span)
}

/** 第 index 格的**起点**（epoch 网格）。 */
export function gridStart(index, { bucketS } = {}) {
  const i = toFiniteOrNull(index)
  const span = toFiniteOrNull(bucketS)
  if (i == null || span == null || span <= 0) return null
  return i * span
}

/**
 * 把时刻**吸附到最近那一格的中心**（`(index + 0.5) * bucket_s`）。
 *
 * ## 为什么是"中心"而不是"起点"
 *
 * "高亮哪一格"要能**纯粹由锚点算出来**（不变式 3）：`floor(anchor / span)`
 * 必须在格子内部任意位置都给出同一个下标。
 *
 * * 吸到**起点**是不行的：`floor` 在起点上是"正好这一格"，但只要浮点尾差让它
 *   比起点小一丁点，就掉到**上一格** —— 这正是历史上那个 off-by-one 的成因。
 * * 吸到**中心**则两边各留半格余量：无论锚点落在格内哪个位置，`floor` 与
 *   "它在哪一格"永远一致。
 *
 * 另一条好处：把窗口做成"锚点 ± 半格"时，窗口两端正好落在**格边界**上
 * （中心 − span/2 = 格首，中心 + span/2 = 格尾）。
 *
 * 吸附是**幂等**的：已经落在中心的时刻再吸附还是它自己（`bucketIndexOf` 依赖这条）。
 * 返回 `null` 表示"没有网格可吸附"（还没拿到 `/api/density` 的 `bucket_s`）。
 */
export function snapToBucket(ts, { bucketS } = {}) {
  const value = toFiniteOrNull(ts)
  const span = toFiniteOrNull(bucketS)
  if (value == null || span == null || span <= 0) return null
  // 先 `floor` 定格（与 `gridIndexOf` 同一套判定），再加半格取中心。
  //
  // ⚠️ 三个"看起来也行"的写法都被实测否掉了（这里踩过一轮，别再改回去）：
  //   * `floor(v/span) * span`（吸到起点）：尾差会让 `floor` 掉到上一格；
  //   * `(round((v-lo)/span) + 0.5) * span`：对**已经落在中心**的值不幂等
  //     （`round(2.5) = 3` ⇒ 中心被推到下一格中心），而幂等是 `bucketIndexOf` 的前提；
  //   * 用 `range.lo` 当原点：那是数据起点、不是桶边界，会系统性偏格（见上面的说明）。
  const index = Math.floor(value / span)
  return (index + 0.5) * span
}

/**
 * 把某个时刻转成"它在第几格" —— **先吸附再算**，因此对浮点尾差免疫。
 *
 * 与 `gridIndexOf` 对**已经吸附**（落在格中心）的锚点结果完全一致（有用例钉住）。
 */
export function bucketIndexOf(ts, { bucketS } = {}) {
  const snapped = snapToBucket(ts, { bucketS })
  const span = toFiniteOrNull(bucketS)
  if (snapped == null || span == null || span <= 0) return -1
  return Math.floor(snapped / span)
}

/**
 * 开发期自检（**只在真的坏了时报警一次**）。
 *
 * 为什么需要：历史上有两次缺陷是"注释里写着必须怎样、代码却悄悄违反"
 * （`detached` 的两种语义、"留言最多的就是主播"）。这条断言把
 * **不变式 2/3** 变成运行期可见的事实 —— 一旦高亮下标与锚点算出来的不一致，
 * 控制台立刻说话，而不是等用户发现"高亮偏了"。
 *
 * 生产环境不引入额外开销：判不成立才拼字符串。
 */
export function assertHighlightConsistent(window, highlightIndex, { bars, bucketS } = {}) {
  const expected = highlightIndexOf(window, { bars, bucketS })
  if (expected === highlightIndex) return true
  // eslint-disable-next-line no-console
  console.warn(
    '[timeline] 高亮下标与锚点不一致（不变式被破坏）：' +
      `高亮=${highlightIndex}，按 anchorTs 算应为 ${expected}；` +
      `anchorTs=${window?.anchorTs} bucket_s=${bucketS} bars=${(bars ?? []).length}`,
  )
  return false
}
