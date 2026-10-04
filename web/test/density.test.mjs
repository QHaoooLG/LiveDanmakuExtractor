/**
 * 时间轴组件的**交互**测试（优化项 ⑨ + ⑩）。
 *
 * 测的是**挂载之后才成立**的东西：鼠标点在某处组件有没有把正确的时间点 emit 出去、
 * 滚轮推不推得动、切换渲染方式的开关管不管用。纯函数已经在
 * `timeline.test.mjs` 里测过，这里不重复。
 *
 * 组件由 `test/vue-loader.mjs` 现编（客户端模式），所以文件里可以直接
 * `import Xxx from '../src/components/Xxx.vue'`。`setupDom()` 必须在**所有**
 * Vue 相关 import 之前跑完 —— 所以组件用动态 `import()` 加载。
 */

import assert from 'node:assert/strict'
import test from 'node:test'

import {
  makeDensity,
  mountComponent,
  restoreCanvasPrototype,
  setBox,
  setupDom,
  stubCanvasPrototype,
} from './helpers/vue.mjs'

setupDom()

const DensityCanvas = (await import('../src/components/DensityCanvas.vue')).default
const DensityDom = (await import('../src/components/DensityDom.vue')).default
const DensityBar = (await import('../src/components/DensityBar.vue')).default

/** 超过 `SCROLL_BARS`(600) 的桶数，用于触发可滚动档。 */
const SCROLL_COUNT = 700
const RENDER_KEY = 'bili-danmaku.timeline-render'

/**
 * 挂一个 canvas 版，并把"量出来的宽度"按下去。
 *
 * 两个环境事实决定了这里的写法：
 *
 * 1. jsdom 不做布局（`clientWidth` 恒为 0），而组件在宽度为 0 时**故意不画**；
 *    `setBox()` 会顺带让 `ResizeObserver` 回调一次（见 `helpers/vue.mjs`），
 *    组件这才量到真宽度，并在下一个 tick 重画。
 * 2. 2D 上下文替身必须**在挂载前**就装到 `prototype` 上 —— 组件在 `onMounted`
 *    里就画了第一次，挂完再注入就看不到那一次。
 */
async function mountCanvas(props = {}, { viewportWidth = 600, scrollWidth } = {}) {
  const ctx = stubCanvasPrototype()
  const wrapper = await mountComponent(
    DensityCanvas,
    { density: makeDensity({ count: 10 }), ...props },
    { emits: ['jump', 'update:bucket', 'update:renderer'] },
  )
  const scroller = wrapper.find('.track-scroll').element
  setBox(scroller, {
    clientWidth: viewportWidth,
    clientHeight: 46,
    scrollWidth: scrollWidth ?? viewportWidth,
  })
  await wrapper.nextTick()
  const canvas = wrapper.find('canvas').element
  return { wrapper, scroller, canvas, ctx }
}

// --------------------------------------------------------------------------- //
// DensityCanvas：表头与共享文案
// --------------------------------------------------------------------------- //

test('canvas 版渲染共用表头：概要行 + 五个格宽档位', async () => {
  const { wrapper } = await mountCanvas()
  assert.match(wrapper.text(), /时间轴/)
  assert.match(wrapper.text(), /每格 5 分钟/)
  const labels = wrapper.findAll('.mini').map((button) => button.text().trim())
  assert.deepEqual(labels, ['1 分钟', '3 分钟', '5 分钟', '10 分钟', '15 分钟', '改用 DOM'])
  wrapper.unmount()
})

test('点格宽档位 → emit update:bucket（值是秒数，不是字符串）', async () => {
  const { wrapper } = await mountCanvas()
  await wrapper.findAll('.mini')[0].trigger('click')
  assert.deepEqual(wrapper.emitted('update:bucket'), [[60]])
  wrapper.unmount()
})

test('canvas 只有一张，不再是"每格一个按钮"', async () => {
  const { wrapper } = await mountCanvas()
  assert.equal(wrapper.findAll('canvas').length, 1)
  assert.equal(wrapper.findAll('.bar-slot').length, 0)
  wrapper.unmount()
})

// --------------------------------------------------------------------------- //
// DensityCanvas：绘制
// --------------------------------------------------------------------------- //

test('量到宽度后按 DPR 设后备缓冲，并画满每一格', async () => {
  const { wrapper, canvas, ctx } = await mountCanvas({}, { viewportWidth: 600 })
  assert.equal(ctx.calls.setTransform, 1)
  assert.equal(ctx.calls.clearRect, 1)
  // 每一格都要画背景，空格子也得点得中
  const background = ctx.ops.filter((op) => op.op === 'fillRect' && op.y === 0)
  assert.equal(background.length, 10)
  assert.ok(canvas.width >= 600, `后备缓冲应不小于 CSS 宽，实际 ${canvas.width}`)
  wrapper.unmount()
})

test('宽度还没量出来时不画（而不是画一堆 NaN）', async () => {
  const ctx = stubCanvasPrototype()
  const wrapper = await mountComponent(DensityCanvas, { density: makeDensity({ count: 10 }) })
  // 刻意**不** setBox：jsdom 下 clientWidth 就是 0 ⇒ 连清屏都不该发生
  assert.equal(ctx.calls.clearRect + ctx.calls.fillRect, 0)
  wrapper.unmount()
  restoreCanvasPrototype()
})

test('拿不到 2d 上下文（jsdom 默认）时不抛异常，跳转照常可用', async () => {
  // 这里**撤掉**替身：`getContext('2d')` 就是 null，走降级分支
  restoreCanvasPrototype()
  const wrapper = await mountComponent(
    DensityCanvas,
    { density: makeDensity({ count: 10 }) },
    { emits: ['jump'] },
  )
  const scroller = wrapper.find('.track-scroll').element
  setBox(scroller, { clientWidth: 600, clientHeight: 46, scrollWidth: 600 })
  await wrapper.nextTick()
  assert.equal(wrapper.find('canvas').element.getContext('2d'), null)
  await wrapper.find('.track-scroll').trigger('click', { clientX: 30 })
  assert.equal(wrapper.emitted('jump')?.length, 1, '画不出来也必须还能点着跳')
  wrapper.unmount()
})

test('focusTs 命中哪一格，就只有那一格用焦点底色（不能高亮错格）', async () => {
  const density = makeDensity({ count: 10 })
  const index = 3
  const focusTs = density.buckets[index].ts + 5
  const { wrapper, ctx } = await mountCanvas({ density, focusTs })
  // 焦点底色 `#24384a` 只该出现在一格上，且那一格的 x 必须对得上第 index 格
  const focused = ctx.ops.filter((op) => op.op === 'fillRect' && op.fill === '#24384a')
  assert.equal(focused.length, 1, `焦点格应当只有一格：${JSON.stringify(focused)}`)
  // 每格 pitch = (600 - 9 个 1px 间隙) / 10 + 1 = 60.1，与 timeline.slotGeometry 一致
  const pitch = (600 - 9) / 10 + 1
  assert.ok(
    Math.abs(focused[0].x - index * pitch) < 1e-6,
    `焦点格 x=${focused[0].x} 与第 ${index} 格（x=${index * pitch}）不符`,
  )
  wrapper.unmount()
})

test('focusTs 落在**有数据**的那一格时，柱子用强调色画（而不只是底色变了）', async () => {
  const density = makeDensity({ count: 10, counts: [5, 5, 5, 5, 5, 5, 5, 5, 5, 5] })
  const focusTs = density.buckets[2].ts + 5
  const { wrapper, ctx } = await mountCanvas({ density, focusTs })
  const accent = ctx.ops.filter((op) => op.op === 'fillRect' && op.fill === '#7dd3fc')
  assert.equal(accent.length, 1, `应当只有一格用强调色：${JSON.stringify(accent)}`)
  wrapper.unmount()
})

test('全部桶都是 0 时仍然逐格画出背景（空格子也是可点的跳转目标）', async () => {
  const empty = makeDensity({ count: 8, counts: Array(8).fill(0) })
  const { wrapper, ctx } = await mountCanvas({ density: empty })
  const background = ctx.ops.filter((op) => op.op === 'fillRect' && op.y === 0)
  assert.equal(background.length, 8)
  // 一根柱子都不该画：有数据才画柱（哪怕高度只有 4%）
  assert.equal(ctx.ops.filter((op) => op.op === 'fillRect' && op.y > 0).length, 0)
  wrapper.unmount()
})

test('没有数据时显示"暂无密度数据"而不是一条空轴', async () => {
  const { wrapper } = await mountCanvas({ density: makeDensity({ count: 0, counts: [] }) })
  assert.match(wrapper.text(), /暂无密度数据/)
  wrapper.unmount()
})

// --------------------------------------------------------------------------- //
// DensityCanvas：交互（命中测试 + 滚轮）
// --------------------------------------------------------------------------- //

test('点击把像素位置换算成桶，emit 出那一格的时间点', async () => {
  const density = makeDensity({ count: 10 })
  const { wrapper } = await mountCanvas({ density })
  // 宽 600、10 格、gap 1 → 每格 59.1 + 1；第 3 格中心约在 3*60.1 + 30 = 210
  await wrapper.find('.track-scroll').trigger('click', { clientX: 210 })
  const jumps = wrapper.emitted('jump')
  assert.equal(jumps?.length, 1)
  assert.equal(jumps[0][0].bucketS, 300)
  const index = density.buckets.findIndex((bucket) => bucket.ts === jumps[0][0].ts)
  assert.equal(index, 3, `应当命中第 3 格，实际 ts=${jumps[0][0].ts}`)
  wrapper.unmount()
})

test('点击**加上 scrollLeft** 才算得对（滚动后最容易错的一处）', async () => {
  const density = makeDensity({ count: SCROLL_COUNT })
  const { wrapper, scroller } = await mountCanvas(
    { density },
    { viewportWidth: 600, scrollWidth: SCROLL_COUNT * 15 },
  )
  scroller.scrollLeft = 1500
  await wrapper.find('.track-scroll').trigger('click', { clientX: 0 })
  const ts = wrapper.emitted('jump')[0][0].ts
  // scrollLeft=1500、pitch=15 → 内容坐标 1500 → 第 100 格
  assert.equal(ts, density.buckets[100].ts)
  wrapper.unmount()
})

test('点在内容右侧的空白处不 emit（不该莫名跳到最后一格）', async () => {
  // 可滚动模式下内容宽是明确的 `格数 × 15`：4 格 = 60px，
  // 而容器 600px —— 612 之后都是死区。用不可滚动模式测不出这件事，
  // 因为那种模式下容器宽**就是**内容宽，根本没有死区。
  const { wrapper } = await mountCanvas(
    { density: makeDensity({ count: 4 }) },
    { viewportWidth: 600, scrollWidth: 900 },
  )
  const event = await wrapper.find('.track-scroll').trigger('click', { clientX: 620 })
  assert.equal(
    wrapper.emitted('jump'),
    undefined,
    `死区不该 emit，实际 ${JSON.stringify(wrapper.emitted('jump'))}`,
  )
  assert.equal(event.defaultPrevented, false)
  wrapper.unmount()
})

test('鼠标移动更新 title（悬停提示：时间区间 + 条数）', async () => {
  const { wrapper } = await mountCanvas({ density: makeDensity({ count: 10 }) })
  const scroller = wrapper.find('.track-scroll')
  assert.equal(wrapper.find('canvas').attributes('title'), '')
  await scroller.trigger('mousemove', { clientX: 30 })
  const title = wrapper.find('canvas').attributes('title')
  assert.match(title, /条$/)
  assert.match(title, /–/)
  await scroller.trigger('mouseleave')
  assert.equal(wrapper.find('canvas').attributes('title'), '')
  wrapper.unmount()
})

test('桶数超过阈值时才可滚动，并且滚轮真的滚得动', async () => {
  const density = makeDensity({ count: SCROLL_COUNT })
  const { wrapper, scroller } = await mountCanvas(
    { density },
    { viewportWidth: 600, scrollWidth: SCROLL_COUNT * 15 },
  )
  assert.ok(wrapper.find('.track-scroll').classes().includes('scrollable'))
  assert.match(wrapper.text(), /可横向滚动/)
  await wrapper.find('.track-scroll').trigger('wheel', { deltaY: 120 })
  assert.equal(scroller.scrollLeft, 120, '竖直滚轮直接当横向位移')
  wrapper.unmount()
})

test('滚到两端时放行滚轮（不 preventDefault，否则用户以为页面卡住）', async () => {
  const density = makeDensity({ count: SCROLL_COUNT })
  const { wrapper, scroller } = await mountCanvas(
    { density },
    { viewportWidth: 600, scrollWidth: SCROLL_COUNT * 15 },
  )
  scroller.scrollLeft = 0
  const event = await wrapper.find('.track-scroll').trigger('wheel', { deltaY: -120 })
  assert.equal(scroller.scrollLeft, 0)
  assert.equal(event.defaultPrevented, false, '到尽头就不该拦截')
  wrapper.unmount()
})

test('桶数不多时不可滚动，滚轮不拦截', async () => {
  const { wrapper } = await mountCanvas({ density: makeDensity({ count: 10 }) })
  assert.equal(wrapper.find('.track-scroll').classes().includes('scrollable'), false)
  const event = await wrapper.find('.track-scroll').trigger('wheel', { deltaY: 120 })
  assert.equal(event.defaultPrevented, false)
  wrapper.unmount()
})

// --------------------------------------------------------------------------- //
// DOM 版（回滚路径）：同一份数值 + 真的可点
// --------------------------------------------------------------------------- //

test('DOM 版每格一个按钮，title 与 canvas 版同源', async () => {
  const density = makeDensity({ count: 10 })
  const wrapper = await mountComponent(DensityDom, { density })
  const slots = wrapper.findAll('.bar-slot')
  assert.equal(slots.length, 10)
  assert.match(String(slots[1].attributes('title')), /条$/)
  wrapper.unmount()
})

test('DOM 版点某一格 emit 出同一格的时间点', async () => {
  const density = makeDensity({ count: 10 })
  const wrapper = await mountComponent(DensityDom, { density }, { emits: ['jump'] })
  await wrapper.findAll('.bar-slot')[3].trigger('click')
  assert.equal(wrapper.emitted('jump')[0][0].ts, density.buckets[3].ts)
  wrapper.unmount()
})

test('DOM 版没有数据时显示"暂无密度数据"', async () => {
  const wrapper = await mountComponent(DensityDom, { density: makeDensity({ count: 0, counts: [] }) })
  assert.match(wrapper.text(), /暂无密度数据/)
  wrapper.unmount()
})

// --------------------------------------------------------------------------- //
// 门面：默认 canvas + 可回滚到 DOM + 记住选择
// --------------------------------------------------------------------------- //

test('门面默认走 canvas 版', async () => {
  window.localStorage.clear()
  const wrapper = await mountComponent(DensityBar, { density: makeDensity({ count: 10 }) })
  assert.equal(wrapper.findAll('canvas').length, 1)
  assert.equal(wrapper.findAll('.bar-slot').length, 0)
  wrapper.unmount()
})

test('点「改用 DOM」当场回滚到旧实现，并把选择写进 localStorage', async () => {
  window.localStorage.clear()
  const wrapper = await mountComponent(DensityBar, { density: makeDensity({ count: 10 }) })
  const switchButton = wrapper.findAll('.mini').find((button) => button.text().trim() === '改用 DOM')
  assert.ok(switchButton, 'canvas 版必须给出回滚入口')
  await switchButton.trigger('click')
  assert.equal(wrapper.findAll('canvas').length, 0, '切过去之后画布必须消失')
  assert.equal(wrapper.findAll('.bar-slot').length, 10)
  assert.equal(window.localStorage.getItem(RENDER_KEY), 'dom')
  wrapper.unmount()
})

test('切回 canvas 同样有效（开关是双向的，不是单向门）', async () => {
  window.localStorage.setItem(RENDER_KEY, 'dom')
  const wrapper = await mountComponent(DensityBar, { density: makeDensity({ count: 10 }) })
  assert.equal(wrapper.findAll('.bar-slot').length, 10)
  const back = wrapper.findAll('.mini').find((button) => button.text().trim() === '改用 canvas')
  await back.trigger('click')
  assert.equal(wrapper.findAll('canvas').length, 1)
  assert.equal(window.localStorage.getItem(RENDER_KEY), 'canvas')
  wrapper.unmount()
})

test('重新挂载时沿用上次的选择（刷新后不会偷偷切回去）', async () => {
  window.localStorage.setItem(RENDER_KEY, 'dom')
  const wrapper = await mountComponent(DensityBar, { density: makeDensity({ count: 10 }) })
  assert.equal(wrapper.findAll('.bar-slot').length, 10)
  wrapper.unmount()
})

test('偏好值不认识时回 canvas（store 里可能是旧版/手改的值）', async () => {
  window.localStorage.setItem(RENDER_KEY, 'webgl')
  const wrapper = await mountComponent(DensityBar, { density: makeDensity({ count: 10 }) })
  assert.equal(wrapper.findAll('canvas').length, 1)
  wrapper.unmount()
})

test('localStorage 抛异常时仍然能用（隐私模式不该让时间轴消失）', async () => {
  const original = window.localStorage
  Object.defineProperty(window, 'localStorage', {
    configurable: true,
    get() {
      throw new Error('SecurityError')
    },
  })
  try {
    const wrapper = await mountComponent(DensityBar, { density: makeDensity({ count: 10 }) })
    assert.equal(wrapper.findAll('canvas').length, 1)
    wrapper.unmount()
  } finally {
    Object.defineProperty(window, 'localStorage', { configurable: true, value: original })
  }
})

test('两版渲染出的**文案完全一致**（否则用户切一次就会看到不同的数字）', async () => {
  const density = makeDensity({ count: 10 })
  const canvasWrapper = await mountComponent(DensityCanvas, { density })
  const domWrapper = await mountComponent(DensityDom, { density })
  const headOf = (wrapper) =>
    wrapper.find('.density-head').text().replace(/改用 (canvas|DOM)/, '').trim()
  assert.equal(headOf(canvasWrapper), headOf(domWrapper))
  canvasWrapper.unmount()
  domWrapper.unmount()
})

test('两版对**同一格**给出的悬停文案完全一致（时间区间与条数同源）', async () => {
  const density = makeDensity({ count: 10 })
  const canvasWrapper = await mountComponent(DensityCanvas, { density })
  const domWrapper = await mountComponent(DensityDom, { density })

  // canvas 版：鼠标移到每一格的区间中心，读 title
  const ctx = stubCanvasPrototype()
  const scroller = canvasWrapper.find('.track-scroll').element
  setBox(scroller, { clientWidth: 600, clientHeight: 46, scrollWidth: 600 })
  await canvasWrapper.nextTick()
  void ctx
  const pitch = (600 - 9) / 10 + 1
  const canvasTitles = []
  for (let index = 0; index < 10; index += 1) {
    await canvasWrapper.find('.track-scroll').trigger('mousemove', { clientX: index * pitch + 1 })
    canvasTitles.push(canvasWrapper.find('canvas').attributes('title'))
  }

  // DOM 版：直接读每一格自己的 title
  const domTitles = domWrapper.findAll('.bar-slot').map((slot) => String(slot.attributes('title')))

  assert.deepEqual(canvasTitles, domTitles)
  assert.ok(canvasTitles.every((title) => /条$/.test(title)), '每格都要有"条数"')
  canvasWrapper.unmount()
  domWrapper.unmount()
})
