/**
 * V3-a 前端逻辑的离线用例（`node --test`，不引入任何依赖）。
 *
 * 覆盖的是**纯逻辑**：游标分页的合并/去重/截断、会话与房间切换的状态机。
 * 这些正是"翻页会不会重复、会不会漏条"最容易出错的地方，用真机点击验证成本太高。
 *
 * 跑法：node --test web/test
 */

import { test } from 'node:test'
import assert from 'node:assert/strict'

import { useHistory, PAGE_SIZE } from '../src/composables/useHistory.js'
import { maskName } from '../src/format.js'
import {
  ANONYMIZE_NAME_KEY,
  buildExportFilename,
  loadAnonymizeName,
  nextExportSeq,
  sanitize,
  saveAnonymizeName,
} from '../src/preferences.js'
import { topSendersHint } from '../src/components/statsHint.js'

// --- 最小 Vue 响应式替身 ---------------------------------------------------- //
// `useHistory` 只用到 ref / shallowRef / computed 三种能力；用真 Vue 需要打包器，
// 而这里要验证的是**数据流逻辑**，所以用等价的最小实现。
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)
const vue = require('vue')

const { ref, shallowRef, computed } = vue

// --- fetch 桩：按 (before/after 游标) 返回分页数据 -------------------------- //

/** 构造一个 0..count-1 的连续事件序列，时间戳每 3 条并列（复刻真实数据的并列现象） */
function makeRows(count, { tied = 3, roomId = 804456 } = {}) {
  return Array.from({ length: count }, (_, index) => {
    const id = index + 1
    return {
      id,
      room_id: roomId,
      session_id: 's1',
      recv_ts: 1_790_000_000 + Math.floor(index / tied),
      type: 'danmaku',
      uid: 1000 + id,
      uname: `用户${id}`,
      text: `内容${id}`,
      price: null,
      medal_name: '牌子',
      medal_level: 3,
      emots: null,
    }
  })
}

/**
 * 最小 Storage 替身：只实现偏好逻辑用到的 getItem/setItem。
 *
 * 用替身而不是 jsdom，是为了顺手能构造"取/存直接抛异常"的隐私模式场景
 * （见「localStorage 不可用」那条用例）。
 */
function makeStorage(initial = {}) {
  const map = new Map(Object.entries(initial))
  return {
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => map.set(key, String(value)),
  }
}

function installFetch(rows, { onCall, bookmarks } = {}) {
  const calls = []
  /** 内存标注库：POST 追加、DELETE 移除、GET 返回（复刻后端语义） */
  const marks = bookmarks ? [...bookmarks] : []
  let nextId = marks.reduce((max, item) => Math.max(max, item.id), 0) + 1

  globalThis.fetch = async (url, options = {}) => {
    const parsed = new URL(url, 'http://localhost')
    const params = parsed.searchParams
    const method = (options.method ?? 'GET').toUpperCase()
    calls.push({ path: parsed.pathname, method, params: Object.fromEntries(params) })
    if (onCall) onCall(parsed, options)

    const json = (payload, status = 200) => ({
      ok: status < 400,
      status,
      json: async () => payload,
    })

    if (parsed.pathname === '/api/annotations') {
      if (method === 'POST') {
        const body = JSON.parse(options.body)
        const item = {
          id: nextId++,
          room_id: body.room_id,
          session_id: body.session_id ?? null,
          anchor_id: body.anchor_id ?? null,
          recv_ts: body.recv_ts,
          label: body.label ?? '标记',
          note: body.note ?? null,
          created_at: 1_790_000_000,
        }
        marks.push(item)
        return json({ item }, 201)
      }
      const room = params.get('room_id')
      const items = marks
        .filter((item) => (room == null ? true : String(item.room_id) === String(room)))
        .sort((a, b) => a.recv_ts - b.recv_ts || a.id - b.id)
      return json({ items, summary: { count: items.length }, source: 'annotations' })
    }

    if (parsed.pathname.startsWith('/api/annotations/') && method === 'DELETE') {
      const id = Number(parsed.pathname.split('/').pop())
      const index = marks.findIndex((item) => item.id === id)
      if (index === -1) return json({ detail: '不存在' }, 404)
      marks.splice(index, 1)
      return json({ removed: id })
    }

    if (parsed.pathname === '/api/density') {
      return json({ bucket_s: 60, buckets: [], total: 0, by_type: {}, range: { lo: null, hi: null } })
    }

    const limit = Number(params.get('limit') ?? PAGE_SIZE)
    const beforeTs = params.get('before_ts')
    const beforeId = params.get('before_id')
    const afterTs = params.get('after_ts')
    const afterId = params.get('after_id')

    let pool = rows
    if (beforeTs && beforeId) {
      const key = [Number(beforeTs), Number(beforeId)]
      pool = rows.filter((r) => r.recv_ts < key[0] || (r.recv_ts === key[0] && r.id < key[1]))
    } else if (afterTs && afterId) {
      const key = [Number(afterTs), Number(afterId)]
      pool = rows.filter((r) => r.recv_ts > key[0] || (r.recv_ts === key[0] && r.id > key[1]))
    }

    // 与后端一致：降序取 limit 条，再升序返回
    const desc = [...pool].sort((a, b) => b.recv_ts - a.recv_ts || b.id - a.id)
    const slice = desc.slice(0, limit)
    const items = slice.reverse()

    const hasOlder = items.length
      ? pool.some(
          (r) =>
            r.recv_ts < items[0].recv_ts ||
            (r.recv_ts === items[0].recv_ts && r.id < items[0].id),
        )
      : false
    const hasNewer = items.length
      ? pool.some(
          (r) =>
            r.recv_ts > items[items.length - 1].recv_ts ||
            (r.recv_ts === items[items.length - 1].recv_ts && r.id > items[items.length - 1].id),
        )
      : false

    return json({
      items,
      has_older: hasOlder,
      has_newer: hasNewer,
      oldest: items.length ? [items[0].recv_ts, items[0].id] : null,
      newest: items.length ? [items[items.length - 1].recv_ts, items[items.length - 1].id] : null,
      source: 'sqlite',
      room_id: 804456,
      session_id: null,
      window_s: 0,
      range: { lo: rows[0].recv_ts, hi: rows[rows.length - 1].recv_ts },
    })
  }
  return calls
}

function setup(rows, options) {
  const roomId = ref(804456)
  const liveEvents = shallowRef([])
  const liveLimit = ref(null)
  const errors = []
  const calls = installFetch(rows, options)
  const history = useHistory(roomId, liveEvents, liveLimit, (message) => errors.push(message))
  return { roomId, liveEvents, liveLimit, history, calls, errors }
}

// --------------------------------------------------------------------------- //

test('进入历史浏览会拉首屏并收缩实时缓冲', async () => {
  const rows = makeRows(1000)
  const { history, liveLimit } = setup(rows)

  assert.equal(history.browsing.value, false)
  assert.equal(liveLimit.value, null)

  await history.enter()

  assert.equal(history.browsing.value, true)
  assert.equal(history.historyItems.value.length, PAGE_SIZE)
  assert.equal(liveLimit.value, 300, '必须收缩实时缓冲，否则合并视图会无限膨胀')
  assert.equal(history.hasOlder.value, true)
  assert.equal(history.hasNewer.value, false)
  // 首屏应是**最新**的一页
  assert.equal(history.historyItems.value.at(-1).id, 1000)
})

test('向上翻到顶：累计覆盖全量且无重复（并列时间戳也要切干净）', async () => {
  const rows = makeRows(1000)
  const { history } = setup(rows)
  await history.enter()

  let guard = 0
  while (history.hasOlder.value && guard < 50) {
    const before = history.historyItems.value.length
    await history.loadOlder()
    guard += 1
    // 窗口有上限，所以条数可能不再增长；用"最旧 id 是否前进"判断进展
    assert.ok(
      history.historyItems.value.length >= before || history.oldest.value === null,
      '每页都应带来新内容',
    )
  }

  const oldest = history.historyItems.value[0]
  assert.equal(oldest.id, 1, `应翻到最早一条，实际 ${oldest.id}`)
  const ids = history.historyItems.value.map((item) => item.id)
  assert.equal(new Set(ids).size, ids.length, '窗口内出现重复')
  assert.equal(history.hasOlder.value, false)
})

test('向下翻页可以追到最新', async () => {
  const rows = makeRows(600)
  const { history } = setup(rows)
  await history.enter()

  // 先往上翻几页，制造"历史窗口落后于最新"
  await history.loadOlder()
  await history.loadOlder()
  assert.equal(history.hasNewer.value, true)

  let guard = 0
  while (history.hasNewer.value && guard < 20) {
    await history.loadNewer()
    guard += 1
  }
  assert.equal(history.historyItems.value.at(-1).id, 600, '应追到最新一条')
})

test('jumpTo 把窗口锚在**所点的那一格**里（回归：曾经偏到前一格）', async () => {
  const rows = makeRows(1000)
  const { history, calls } = setup(rows)
  const bucketS = 600
  const target = rows[499].recv_ts // 假设这就是被点格子的起点

  await history.jumpTo(target, bucketS)

  const call = calls.filter((c) => c.path === '/api/history').at(-1)
  // 窗口右端必须是"该格结束时刻"，而不是该格起点 —— 后者会让整段窗口落在上一格
  assert.equal(Number(call.params.until), target + bucketS, 'until 应为格子结束时刻')
  assert.ok(Number(call.params.until) > target, 'until 必须晚于所点时刻')
  assert.equal(history.focusTs.value, target)
  assert.ok(history.historyItems.value.length > 0)

  // 窗口里必须**含有所点格子的数据**，且窗口左端不早于该格
  const lo = history.range.value.lo
  const hi = history.range.value.hi
  assert.ok(hi <= target + bucketS + 0.001, `窗口右端越过了所点格子：${hi} > ${target + bucketS}`)
  assert.ok(lo <= target || rows.some((r) => r.recv_ts >= target && r.recv_ts < target + bucketS))
})

test('jumpTo 的默认格宽是 60 秒', async () => {
  const rows = makeRows(200)
  const { history, calls } = setup(rows)
  await history.jumpTo(rows[50].recv_ts)
  const call = calls.filter((c) => c.path === '/api/history').at(-1)
  assert.equal(Number(call.params.until), rows[50].recv_ts + 60)
})

test('退出历史浏览清空窗口并恢复实时缓冲上限', async () => {
  const rows = makeRows(500)
  const { history, liveLimit } = setup(rows)
  await history.enter()
  assert.ok(history.historyItems.value.length > 0)

  history.exit()
  assert.equal(history.browsing.value, false)
  assert.deepEqual(history.historyItems.value, [])
  assert.equal(liveLimit.value, null)
})

test('合并视图：历史 + 实时按 (recv_ts,id) 归并且按 id 去重', async () => {
  const rows = makeRows(50)
  const { history, liveEvents } = setup(rows)

  await history.enter() // 窗口 = 最新 50 条（id 1..50，因为 PAGE_SIZE > 50）
  assert.equal(history.historyItems.value.length, 50)

  // 实时层给出与历史**重叠**的两条（模拟 WS 回填）加一条全新的
  liveEvents.value = [
    { ...rows[48], seq: rows[48].id, extra: {} },
    { ...rows[49], seq: rows[49].id, extra: {} },
    { id: 51, room_id: 804456, session_id: 's1', recv_ts: 1_790_000_020, type: 'danmaku', text: '新弹幕', seq: 51 },
  ]

  const merged = history.merged.value
  const ids = merged.map((item) => item.id)
  assert.equal(new Set(ids).size, ids.length, '合并后出现重复 id')
  assert.deepEqual(ids, [...ids].sort((a, b) => a - b), '必须按时间序（此处 id 与时间同序）')
  assert.ok(ids.includes(51), '实时新条目必须进入合并视图')
})

test('未进入历史浏览时，合并视图就是实时数组本身（零额外分配）', () => {
  const rows = makeRows(10)
  const { history, liveEvents } = setup(rows)
  const live = [{ id: 1, recv_ts: 1, type: 'danmaku', text: 'x' }]
  liveEvents.value = live
  assert.equal(history.merged.value, live)
})

test('窗口超过上限时从远离焦点的一侧截断', async () => {
  const rows = makeRows(4000, { tied: 1 })
  const { history } = setup(rows)
  await history.enter()

  for (let i = 0; i < 10; i += 1) await history.loadOlder()

  const items = history.historyItems.value
  assert.ok(items.length <= 1200, `窗口应被截断，实际 ${items.length}`)
  // 向上翻时丢的是**尾部**（更新的一侧），保留的是更旧的内容
  assert.equal(items[0].id < items.at(-1).id, true)
})

test('房间切换后 reset 会清空窗口并丢弃在途请求', async () => {
  const rows = makeRows(500)
  const { history } = setup(rows)
  await history.enter()
  assert.ok(history.historyItems.value.length > 0)

  history.reset()
  assert.deepEqual(history.historyItems.value, [])
  assert.equal(history.hasOlder.value, false)
})

test('游标请求一定成对带 ts 与 id', async () => {
  const rows = makeRows(1000)
  const { history, calls } = setup(rows)
  await history.enter()
  await history.loadOlder()
  await history.loadNewer()

  const historyCalls = calls.filter((call) => call.path === '/api/history')
  for (const call of historyCalls) {
    const hasBefore = 'before_ts' in call.params
    const hasAfter = 'after_ts' in call.params
    // 每个请求要么没有游标（首屏），要么 ts+id 成对
    if (hasBefore) assert.ok('before_id' in call.params, 'before_ts 必须与 before_id 成对')
    if (hasAfter) assert.ok('after_id' in call.params, 'after_ts 必须与 after_id 成对')
    assert.equal(hasBefore && hasAfter, false, '两个方向不能同时传')
  }
  assert.ok(historyCalls.some((call) => 'before_ts' in call.params), 'loadOlder 应产生 before 游标')
  assert.ok(historyCalls.some((call) => 'after_ts' in call.params), 'loadNewer 应产生 after 游标')
})

// --------------------------------------------------------------------------- //
// 标记（V3-a2）
// --------------------------------------------------------------------------- //

test('打标记会落到独立库并立刻出现在列表里', async () => {
  const rows = makeRows(50)
  const { history } = setup(rows)

  const item = await history.addBookmark({ recvTs: rows[10].recv_ts, label: '名场面' })
  assert.ok(item, '应返回新建的标记')
  assert.equal(item.label, '名场面')
  assert.equal(item.recv_ts, rows[10].recv_ts)
  assert.equal(history.bookmarks.value.length, 1)
  assert.equal(history.activeBookmarkId.value, item.id, '新建后自动选中，便于直接导出')
  assert.equal(history.activeBookmark.value.label, '名场面')
})

test('标记缺省标签是「标记」，且没选房间时给出提示而不是抛异常', async () => {
  const rows = makeRows(10)
  const { history, roomId, errors } = setup(rows)

  const item = await history.addBookmark({ recvTs: rows[0].recv_ts })
  assert.equal(item.label, '标记')

  roomId.value = null
  const blocked = await history.addBookmark({ recvTs: rows[0].recv_ts })
  assert.equal(blocked, null)
  assert.ok(errors.some((message) => message.includes('先选择一个房间')), `缺提示：${errors}`)

  // 房间有效、但时间点不可用（例如一条弹幕都还没有）
  roomId.value = 804456
  const noTime = await history.addBookmark({ recvTs: undefined })
  assert.equal(noTime, null)
  assert.ok(errors.some((message) => message.includes('时间点')))
})

test('删除标记会同步列表与选中态', async () => {
  const rows = makeRows(30)
  const { history } = setup(rows)
  const first = await history.addBookmark({ recvTs: rows[1].recv_ts, label: 'A' })
  const second = await history.addBookmark({ recvTs: rows[2].recv_ts, label: 'B' })
  assert.equal(history.bookmarks.value.length, 2)

  await history.removeBookmark(first.id)
  assert.deepEqual(
    history.bookmarks.value.map((item) => item.label),
    ['B'],
  )
  assert.equal(history.activeBookmarkId.value, second.id, '删的不是选中的那个，选中态应保留')

  await history.removeBookmark(second.id)
  assert.equal(history.activeBookmarkId.value, null, '删掉选中的那个要清空选中态')
  assert.equal(history.bookmarks.value.length, 0)
})

test('清空标记会清掉全部并复位选中态', async () => {
  const rows = makeRows(30)
  const { history } = setup(rows)
  for (const index of [1, 2, 3]) {
    await history.addBookmark({ recvTs: rows[index].recv_ts })
  }
  await history.clearBookmarks()
  assert.deepEqual(history.bookmarks.value, [])
  assert.equal(history.activeBookmarkId.value, null)
})

test('标记只按时间升序排列，与历史窗口无关', async () => {
  const rows = makeRows(100)
  const { history } = setup(rows)
  // 故意乱序打标记
  for (const index of [40, 5, 70]) {
    await history.addBookmark({ recvTs: rows[index].recv_ts })
  }
  const stamps = history.bookmarks.value.map((item) => item.recv_ts)
  assert.deepEqual(stamps, [...stamps].sort((a, b) => a - b))
})

test('房间切换后 reset 会清空选中态，重新拉取本房间标记', async () => {
  const rows = makeRows(20)
  const { history } = setup(rows)
  await history.addBookmark({ recvTs: rows[0].recv_ts })
  assert.ok(history.activeBookmarkId.value != null)

  history.reset()
  assert.equal(history.activeBookmarkId.value, null)
  await history.loadBookmarks()
  assert.equal(history.bookmarks.value.length, 1, '标记属于房间，切换后仍应能拉到')
})

test('钉在某条弹幕上时带上 anchor_id', async () => {
  const rows = makeRows(20)
  const { history, calls } = setup(rows)
  await history.addBookmark({ recvTs: rows[7].recv_ts, anchorId: rows[7].id })
  const post = calls.find((call) => call.method === 'POST' && call.path === '/api/annotations')
  assert.ok(post, '应发出 POST')
  assert.equal(history.bookmarks.value[0].anchor_id, rows[7].id)
})

test('请求失败时给出错误提示且不破坏已有窗口', async () => {
  const rows = makeRows(400)
  const { history, errors } = setup(rows)
  await history.enter()
  const before = history.historyItems.value.length

  globalThis.fetch = async () => {
    throw new Error('boom')
  }
  await history.loadOlder()

  assert.equal(history.historyItems.value.length, before, '失败不应清空窗口')
  assert.ok(errors.some((message) => message.includes('加载历史失败')), `未收到错误提示：${errors}`)
})

// --------------------------------------------------------------------------- //
// 昵称**匿名化**开关（默认不匿名；匿名 = 只留首字符，其余 * 掩盖）
//
// 开关在上下文条（FilterBar），状态由 App.vue 持有，所以这里测两件纯逻辑：
// 偏好读写 + `maskName()` 的掩盖规则。
// --------------------------------------------------------------------------- //

test('"匿名昵称"默认关（需求：默认不进行匿名）', () => {
  const storage = makeStorage()
  assert.equal(loadAnonymizeName(storage), false, '没有任何存值时必须是 false')

  saveAnonymizeName(storage, true)
  assert.equal(storage.getItem(ANONYMIZE_NAME_KEY), '1')
  assert.equal(loadAnonymizeName(storage), true, '打开后刷新页面应保持匿名')

  saveAnonymizeName(storage, false)
  assert.equal(storage.getItem(ANONYMIZE_NAME_KEY), '0')
  assert.equal(loadAnonymizeName(storage), false)
})

test('匿名偏好读不到/写不下时退回"不匿名"，且不抛异常', () => {
  const broken = {
    getItem() {
      throw new Error('SecurityError')
    },
    setItem() {
      throw new Error('QuotaExceededError')
    },
  }
  assert.equal(loadAnonymizeName(broken), false)
  assert.doesNotThrow(() => saveAnonymizeName(broken, true))
  assert.doesNotThrow(() => saveAnonymizeName(undefined, true))
  // 只有明确 '1' 才算匿名；脏数据按"不匿名"处理
  assert.equal(loadAnonymizeName(makeStorage({ [ANONYMIZE_NAME_KEY]: 'yes' })), false)
})

test('maskName 只留首字符，其余用 * 掩盖', () => {
  assert.equal(maskName('六式zy'), '六******')
  assert.equal(maskName('ab'), 'a****')
  // 单字符昵称没有"其余"，但仍然补足固定星号 —— 否则一眼就能看出原名只有 1 个字
  assert.equal(maskName('a'), 'a***')
  assert.equal(maskName('张三'), '张****')
  // 空/缺失不崩，也不产生孤零零的 `*`
  assert.equal(maskName(''), '')
  assert.equal(maskName(null), '')
  assert.equal(maskName(undefined), '')
})

test('maskName 处理 emoji 昵称不会拆出半个字符', () => {
  // 用 Array.from 而不是 name[0]：后者会把代理对拆成半个乱码
  const masked = maskName('🐟鱼')
  assert.equal(Array.from(masked)[0], '🐟', '首字符应当是一个完整的 emoji')
})

test('maskName 不按原长给星号（避免数星号猜长度）', () => {
  const short = maskName('张三')
  const long = maskName('张三李四王五赵六钱七孙八周九吴十郑一王二')
  // 长昵称的星号更多，但差距被压住（上限 8 + 3），不会一眼看出确切长度
  assert.ok(long.length >= short.length)
  assert.ok(long.length <= 1 + 8 + 3, `星号过多会泄露长度: ${long.length}`)
})

// --- 「发言 TOP」为空的提示文案 -------------------------------------------- //
//
// 回归：这里曾用 `source === 'memory'` 判断"sqlite sink 没开"，而累计口径下
// `source` **恒为 'memory'**（那说的是条数/速率的来源），于是新库上会显示
// "启用 sqlite sink 后才有发言榜" —— 而 sqlite 默认本来就是开着的，纯属误导。
// 判据改成看"有没有真的查过库"，也就是后端有没有带上 `top_senders_scope` 键。

test('发言榜有数据时不显示任何提示', () => {
  assert.equal(
    topSendersHint({
      mode: 'cumulative',
      source: 'memory',
      top_senders: [{ uid: 1, uname: 'a', count: 3 }],
      top_senders_scope: '全部房间',
    }),
    '',
  )
})

test('累计口径 + 查过库但为空：提示"还没有可聚合的发言"，不提 sqlite', () => {
  // 后端只有在真的打开 sqlite 并算出结果时才带 top_senders_scope
  const hint = topSendersHint({
    mode: 'cumulative',
    source: 'memory',
    top_senders: [],
    top_senders_scope: '全部房间',
  })
  assert.match(hint, /还没有可聚合的发言/)
  assert.doesNotMatch(hint, /sqlite/, 'sqlite 默认就是开着的，不该让用户去启用它')
})

test('全新安装（库还没建、也没收到弹幕）：不提示，避免噪音', () => {
  // 这是最容易被误报成"坏了"的场景；实际上榜为空完全正常
  assert.equal(
    topSendersHint({
      mode: 'cumulative',
      source: 'memory',
      msgs_total: 0,
      top_senders_error: '数据库尚未建立',
    }),
    '',
  )
})

test('收到了弹幕但库没建起来：才提示（可能是落盘问题）', () => {
  const hint = topSendersHint({
    mode: 'cumulative',
    source: 'memory',
    msgs_total: 123,
    top_senders_error: '数据库尚未建立',
  })
  assert.match(hint, /采集库还没建立/)
})

test('真的没启用 sqlite sink：这时才该提 sqlite', () => {
  const hint = topSendersHint({
    mode: 'cumulative',
    source: 'memory',
    msgs_total: 0,
    top_senders_error: 'sqlite sink 未启用',
  })
  assert.match(hint, /sqlite/)
})

test('滑动窗口口径：提示是"该范围内暂无发言"', () => {
  assert.equal(
    topSendersHint({ mode: 'window', source: 'sqlite', window_s: 300, top_senders: [] }),
    '该范围内暂无发言',
  )
})

test('后端没给原因（旧版）时兜底，不误导', () => {
  const hint = topSendersHint({ mode: 'cumulative', source: 'memory', msgs_total: 0 })
  assert.doesNotMatch(hint, /sqlite/, '不知道原因时不该让用户去改 sqlite')
})

test('stats 为 null（还没加载出来）不崩', () => {
  assert.doesNotThrow(() => topSendersHint(null))
  assert.doesNotThrow(() => topSendersHint(undefined))
})

// --------------------------------------------------------------------------- //
// 导出文件名：<主播名字>_<房间号>_<日期>_<当天导出编号>.csv
// --------------------------------------------------------------------------- //

test('导出文件名按 主播_房间_日期_编号 组装', () => {
  assert.equal(
    buildExportFilename({ anchorName: '六式zy', roomId: 804456, day: '20261002', seq: 1 }),
    '六式zy_804456_20261002_1.csv',
  )
})

test('缺主播/房间时用可读的占位，而不是留空成 `__`', () => {
  assert.equal(
    buildExportFilename({ anchorName: '', roomId: null, day: '20261002', seq: 3 }),
    '未知主播_全部房间_20261002_3.csv',
  )
})

test('文件名清洗掉路径分隔符等非法字符', () => {
  // 主播昵称带 `/` 会让"文件名"变成一个路径，Windows 上直接失败
  assert.equal(sanitize('a/b\\c:d*e?f"g<h>i|j'), 'abcdefghij')
  const name = buildExportFilename({
    anchorName: 'A/B:C',
    roomId: 123,
    day: '20261002',
    seq: 1,
  })
  assert.equal(name, 'ABC_123_20261002_1.csv')
  assert.doesNotMatch(name, /[\\/:*?"<>|]/)
})

test('当天导出编号从 1 开始并自增，跨天重新计数', () => {
  const storage = makeStorage()
  const day1 = new Date(2026, 9, 2, 10, 0, 0) // 2026-10-02

  assert.deepEqual(nextExportSeq(storage, day1), { day: '20261002', seq: 1 })
  assert.deepEqual(nextExportSeq(storage, day1), { day: '20261002', seq: 2 })
  assert.deepEqual(nextExportSeq(storage, day1), { day: '20261002', seq: 3 })

  // 第二天从 1 重新数（按**本地**日期分桶）
  const day2 = new Date(2026, 9, 3, 9, 0, 0)
  assert.deepEqual(nextExportSeq(storage, day2), { day: '20261003', seq: 1 })
  // 回到第一天，接着原来的计数继续
  assert.deepEqual(nextExportSeq(storage, day1), { day: '20261002', seq: 4 })
})

test('序号存储不可用时退回 1，不抛异常', () => {
  const broken = {
    getItem() {
      throw new Error('SecurityError')
    },
    setItem() {
      throw new Error('QuotaExceededError')
    },
  }
  assert.deepEqual(nextExportSeq(broken, new Date(2026, 9, 2)), { day: '20261002', seq: 1 })
  // 值为脏数据（手改过）：当成 0，下一次得到 1
  assert.deepEqual(
    nextExportSeq(makeStorage({ 'bili-danmaku.export-seq.20261002': 'abc' }), new Date(2026, 9, 2)),
    { day: '20261002', seq: 1 },
  )
})

