/**
 * `src/timeline.js` 的单测。
 *
 * 这个模块是 canvas 版与 DOM 版的**唯一数值来源**，所以这里的用例实际上同时在
 * 守着两份渲染实现 —— 它算错了，两种渲染方式会一起错，且看起来"是一致的"，
 * 比两份实现各错一半更难发现。
 */

import assert from 'node:assert/strict'
import test from 'node:test'

import {
  BUCKET_CHOICES,
  DENSE_BARS,
  MIN_SLOT_PX,
  SCROLL_BARS,
  buildBars,
  buildSummary,
  focusIndexOf,
  formatDay,
  formatSpan,
  formatStamp,
  hitTest,
  inWindow,
  peakOf,
  slotGeometry,
  spanOf,
  trackWidth,
  wheelDelta,
} from '../src/timeline.js'

/** 造一个 `/api/density` 响应（与后端形状一致）。 */
function density(counts, { bucketS = 300, start = 1_789_710_000 } = {}) {
  return {
    bucket_s: bucketS,
    range: counts.length
      ? { lo: start, hi: start + (counts.length - 1) * bucketS }
      : { lo: null, hi: null },
    total: counts.reduce((sum, value) => sum + value, 0),
    buckets: counts.map((value, index) => ({ ts: start + index * bucketS, count: value })),
  }
}

// --------------------------------------------------------------------------- //
// 归一化峰值：P95 而不是最大值
// --------------------------------------------------------------------------- //

test('峰值用 P95：一个毛刺不该把其余柱子压平', () => {
  // 99 个 10，1 个 1000。用最大值归一化会把所有柱子压到 1% 高度。
  const counts = [...Array(99).fill(10), 1000]
  const peak = peakOf(counts.map((count) => ({ count })))
  assert.equal(peak, 10, `P95 应该是 10，实际 ${peak}`)
})

test('峰值至少是 1（避免除零）', () => {
  assert.equal(peakOf([]), 1)
  assert.equal(peakOf([{ count: 0 }, { count: 0 }]), 1)
  assert.equal(peakOf(null), 1)
})

test('比例封顶在 1：P95 之外的毛刺不能画出屏幕', () => {
  const bars = buildBars(density([...Array(99).fill(10), 1000]))
  assert.equal(bars.length, 100)
  assert.ok(bars.every((bar) => bar.ratio <= 1))
  assert.equal(bars[bars.length - 1].ratio, 1, '毛刺本身就是 1000/10 → 封顶 1')
})

test('没有桶时给出空数组（组件据此显示"暂无密度数据"）', () => {
  assert.deepEqual(buildBars(density([])), [])
  assert.deepEqual(buildBars(null), [])
})

test('每根柱子都带悬停用的时间区间文案', () => {
  const bars = buildBars(density([1, 2], { bucketS: 300 }))
  assert.match(bars[0].span, /^\d\d:\d\d:\d\d–\d\d:\d\d:\d\d$/)
  assert.notEqual(bars[0].span, bars[1].span)
})

// --------------------------------------------------------------------------- //
// 焦点格判定：区间优先，空档兜底
// --------------------------------------------------------------------------- //

const SPAN = 300
const BARS = [0, 1, 2, 3].map((index) => ({ ts: 1_789_710_000 + index * SPAN, count: 1 }))

test('focusTs 落在某格区间内 → 就是那一格', () => {
  for (const index of [0, 1, 2, 3]) {
    const ts = BARS[index].ts + 10
    assert.equal(focusIndexOf(BARS, ts, SPAN), index)
  }
})

test('焦点在格子**右边界**上归下一格（左闭右开）', () => {
  // 3 格起点恰好是第 2 格的右界：按区间判定应归第 3 格，不该"取最近"偏到第 2 格
  assert.equal(focusIndexOf(BARS, BARS[2].ts, SPAN), 2)
  assert.equal(focusIndexOf(BARS, BARS[3].ts - 0.001, SPAN), 2)
})

test('focusTs 落在没有数据的空档里 → 取时间上最近的一格', () => {
  const gapped = [
    { ts: 1_789_710_000, count: 1 },
    { ts: 1_789_710_000 + SPAN * 10, count: 1 },
  ]
  // 离第一格更近
  assert.equal(focusIndexOf(gapped, 1_789_710_000 + SPAN * 2, SPAN), 0)
  // 离第二格更近
  assert.equal(focusIndexOf(gapped, 1_789_710_000 + SPAN * 9, SPAN), 1)
})

test('没有 focusTs 或没有柱子时返回 -1', () => {
  assert.equal(focusIndexOf(BARS, null, SPAN), -1)
  assert.equal(focusIndexOf([], 1_789_710_000, SPAN), -1)
})

// --------------------------------------------------------------------------- //
// 窗口高亮
// --------------------------------------------------------------------------- //

test('有 focusTs 时不做窗口高亮（点哪格就只亮哪格）', () => {
  const bar = BARS[1]
  assert.equal(inWindow(bar, { focusTs: bar.ts + 5, range: { lo: 0, hi: 9e12 }, spanS: SPAN }), false)
})

test('没有 focusTs 时按窗口区间高亮（含跨界的那一格）', () => {
  const range = { lo: BARS[1].ts + 50, hi: BARS[2].ts + 50 }
  assert.equal(inWindow(BARS[0], { range, spanS: SPAN }), false)
  // 第 1 格的右界伸进了窗口 → 算在窗口内
  assert.equal(inWindow(BARS[1], { range, spanS: SPAN }), true)
  assert.equal(inWindow(BARS[2], { range, spanS: SPAN }), true)
  assert.equal(inWindow(BARS[3], { range, spanS: SPAN }), false)
})

// --------------------------------------------------------------------------- //
// 概要行
// --------------------------------------------------------------------------- //

test('没有数据时概要行说"加载中…"，有响应但无数据时说"该区间内没有数据"', () => {
  assert.equal(buildSummary(null), '加载中…')
  assert.equal(buildSummary(density([])), '该区间内没有数据')
})

test('概要行只放三件事：区间、每格时长、总量', () => {
  const text = buildSummary(density([1, 2, 3], { bucketS: 300 }))
  assert.match(text, /→/)
  assert.match(text, /每格 5 分钟/)
  assert.match(text, /共 6 条/)
  // 曾经把"格数 / 平均条数"也塞进来，反馈是"信息较为混乱" —— 钉住别再回去
  assert.doesNotMatch(text, /格数|平均/)
})

test('不到 1 分钟的格宽按秒显示（不该出现"0.5 分钟"这种读不出来的说法）', () => {
  assert.match(buildSummary(density([1], { bucketS: 30 })), /每格 30 秒/)
  assert.match(buildSummary(density([1], { bucketS: 180 })), /每格 3 分钟/)
})

test('格宽缺省按 60 秒', () => {
  const payload = density([1])
  delete payload.bucket_s
  assert.equal(spanOf(payload), 60)
})

// --------------------------------------------------------------------------- //
// 每格宽度（三种缩放档）
// --------------------------------------------------------------------------- //

test('常规档：留 1px 间隙，整宽平分', () => {
  const { slot, gap } = slotGeometry({ count: 10, viewportWidth: 110, scrollable: false })
  assert.equal(gap, 1)
  // 110 - 9 个间隙 = 101，分给 10 格 → 10.1
  assert.equal(slot, 10.1)
})

test(`超过 ${DENSE_BARS} 格：放弃保底宽度与间隙（宁可压扁也不溢出面板）`, () => {
  const { slot, gap } = slotGeometry({
    count: DENSE_BARS + 100,
    viewportWidth: 1000,
    scrollable: false,
  })
  assert.equal(gap, 0)
  assert.ok(slot * (DENSE_BARS + 100) <= 1000 + 1e-6, '总宽不能超出容器')
})

test(`超过 ${SCROLL_BARS} 格：给每格可点宽度并改为可滚动`, () => {
  const { slot, gap } = slotGeometry({
    count: SCROLL_BARS + 1,
    viewportWidth: 1000,
    scrollable: true,
  })
  assert.equal(slot, MIN_SLOT_PX)
  assert.equal(gap, 1)
  assert.equal(
    trackWidth({ count: SCROLL_BARS + 1, viewportWidth: 1000, scrollable: true }),
    (SCROLL_BARS + 1) * (MIN_SLOT_PX + 1),
  )
})

test('不可滚动时内容宽就是容器宽', () => {
  assert.equal(trackWidth({ count: 10, viewportWidth: 777, scrollable: false }), 777)
})

test('还没量到宽度时给 0（调用方据此跳过绘制，而不是画满是 NaN 的东西）', () => {
  assert.deepEqual(slotGeometry({ count: 10, viewportWidth: 0, scrollable: false }), {
    slot: 0,
    gap: 0,
  })
  assert.deepEqual(slotGeometry({ count: 0, viewportWidth: 100, scrollable: false }), {
    slot: 0,
    gap: 0,
  })
})

test('极窄容器里不产生负宽度', () => {
  const { slot } = slotGeometry({ count: 5, viewportWidth: 3, scrollable: false })
  assert.ok(slot > 0, `格子宽度必须为正，实际 ${slot}`)
})

// --------------------------------------------------------------------------- //
// 命中测试：鼠标位置 → 桶下标
// --------------------------------------------------------------------------- //

test('命中测试按 格宽+间隙 折算下标', () => {
  const opts = { count: 10, slot: 10, gap: 1 }
  assert.equal(hitTest({ ...opts, x: 0 }), 0)
  assert.equal(hitTest({ ...opts, x: 10.9 }), 0, '间隙里也算这一格')
  assert.equal(hitTest({ ...opts, x: 11 }), 1)
  assert.equal(hitTest({ ...opts, x: 109 }), 9)
})

test('超出内容范围不算命中（否则点最右边会莫名跳到最后一格）', () => {
  const opts = { count: 10, slot: 10, gap: 1 }
  assert.equal(hitTest({ ...opts, x: 110 }), null)
  assert.equal(hitTest({ ...opts, x: 5000 }), null)
  assert.equal(hitTest({ ...opts, x: -1 }), null)
})

test('还没量出宽度时不命中（slot 为 0）', () => {
  assert.equal(hitTest({ count: 10, slot: 0, gap: 0, x: 5 }), null)
})

// --------------------------------------------------------------------------- //
// 滚轮
// --------------------------------------------------------------------------- //

test('滚轮位移用 deltaY（鼠标停在窄条上时意图几乎不可能是滚页面）', () => {
  assert.equal(wheelDelta({ deltaY: 40, deltaX: 0, scrollLeft: 100, maxScroll: 900 }), 40)
})

test('deltaY 为 0 时退回 deltaX（触控板横滚）', () => {
  assert.equal(wheelDelta({ deltaY: 0, deltaX: -30, scrollLeft: 100, maxScroll: 900 }), -30)
})

test('已经到两端就放行（返回 0，不 preventDefault）', () => {
  assert.equal(wheelDelta({ deltaY: -40, deltaX: 0, scrollLeft: 0, maxScroll: 900 }), 0)
  assert.equal(wheelDelta({ deltaY: 40, deltaX: 0, scrollLeft: 900, maxScroll: 900 }), 0)
  // 差 1px 也算到尽头：浮点/像素取整会让 scrollLeft 与 max 差一点点
  assert.equal(wheelDelta({ deltaY: 40, deltaX: 0, scrollLeft: 899.5, maxScroll: 900 }), 0)
})

test('不可滚动时不拦截', () => {
  assert.equal(wheelDelta({ deltaY: 40, deltaX: 0, scrollLeft: 0, maxScroll: 0 }), 0)
})

// --------------------------------------------------------------------------- //
// 文案
// --------------------------------------------------------------------------- //

test('时间戳补零到 秒', () => {
  assert.match(formatStamp(1_789_710_000), /^\d\d:\d\d:\d\d$/)
  assert.match(formatDay(1_789_710_000), /^\d+\/\d+ \d\d:\d\d$/)
})

test('时间区间是"起点–终点"（不是"起点+格宽"这种要用户自己算的东西）', () => {
  assert.equal(formatSpan(1_789_710_000, 300), `${formatStamp(1_789_710_000)}–${formatStamp(1_789_710_300)}`)
})

test('格宽档位是升序的，且都不小于 1 分钟', () => {
  const values = BUCKET_CHOICES.map((choice) => choice.value)
  assert.deepEqual(values, [...values].sort((a, b) => a - b))
  assert.ok(values.every((value) => value >= 60))
})
