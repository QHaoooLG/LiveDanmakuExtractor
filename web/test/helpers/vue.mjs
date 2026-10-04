/**
 * 组件测试的公共脚手架（**不用 `@vue/test-utils`**）。
 *
 * ## 为什么自己写这一小层
 *
 * `@vue/test-utils` 的 `mount()` 依赖 Vue 的**内部** `currentInstance` 状态，
 * 而我们在测试进程里必须先用 jsdom 装出 `document` 再 import Vue（`@vue/runtime-dom`
 * 在模块求值那一刻就把 `document` 抓走了）。一旦解析路径不同，就会出现"两份 Vue"
 * 与一堆指不到病根的报错。
 *
 * 这一层只用**公开 API**：`createApp` + `h` + `nextTick`。要断言的东西
 * （渲染出的文本、属性、emit 出的事件、点击/滚轮行为）全都能覆盖 —— 换来的是
 * 没有黑盒、没有版本耦合。`test/vue-loader.mjs` 负责把 `.vue` 编成客户端组件，
 * 所以这里 `import` 出来的就是真组件。
 *
 * ## 顺序是硬要求
 *
 * `mountComponent()` 必须在 `setupDom()` **之后**调用（jsdom 要先就位）。
 * 用法：
 *
 * ```js
 * import { mountComponent, setupDom } from './helpers/vue.mjs'
 * import DensityCanvas from '../src/components/DensityCanvas.vue'
 *
 * setupDom()                     // 只装 jsdom，不 import Vue
 * const wrapper = mountComponent(DensityCanvas, { density })
 * ```
 */

import { JSDOM } from 'jsdom'

let dom = null

/**
 * 装 jsdom 并把它挂到 `globalThis`。
 *
 * **同步**且不 import Vue —— 调用方负责在它之后才动态 import Vue 相关模块，
 * 否则 `@vue/runtime-dom` 会拿到 `document === undefined`。
 */
export function setupDom() {
  if (dom) return dom.window
  dom = new JSDOM('<!doctype html><html><body></body></html>', {
    url: 'http://127.0.0.1:8787/',
    pretendToBeVisual: true,
  })
  const { window } = dom
  const expose = (name, value) => {
    Object.defineProperty(globalThis, name, { value, configurable: true, writable: true })
  }
  expose('window', window)
  expose('document', window.document)
  expose('localStorage', window.localStorage)
  expose('getComputedStyle', window.getComputedStyle.bind(window))
  expose('requestAnimationFrame', window.requestAnimationFrame?.bind(window))
  expose('cancelAnimationFrame', window.cancelAnimationFrame?.bind(window))
  stubResizeObserver()
  // Vue 的 `resolveRootNamespace()` 会直接摸 `SVGElement`（判断挂载点命名空间），
  // jsdom 的构造器**不在** globalThis 上 —— 少一个就是 `ReferenceError`，
  // 而且报错点在 Vue 内部，看起来完全不像"环境没搭好"（实测踩到）。
  //
  // `navigator` 走 `defineProperty`：Node 21+ 的 `globalThis.navigator` 是
  // **只有 getter** 的属性，直接赋值会 TypeError。
  for (const name of Object.getOwnPropertyNames(window)) {
    if (!/^[A-Z]/.test(name)) continue
    if (name in globalThis) continue
    if (typeof window[name] !== 'function') continue
    expose(name, window[name])
  }
  expose('navigator', window.navigator)
  return window
}

/**
 * 让测试**自己控制** `ResizeObserver` 的回调时机。
 *
 * jsdom 没有布局引擎，`clientWidth` 恒为 0 —— 而组件在宽度为 0 时**故意不画**。
 * 所以测试必须先造出尺寸、再让观察者回调一次；而"造尺寸"这件事只有测试知道，
 * 所以只能把触发器交出来。
 *
 * 用法：`setBox(el, { clientWidth: 600 })` 之后回调会**自动**跑一次，
 * 不需要手动 fire。
 */
export function stubResizeObserver() {
  const targets = new Set()
  let callback = null
  class Observer {
    constructor(cb) {
      callback = cb
    }
    observe(target) {
      targets.add(target)
      callback?.([])
    }
    unobserve(target) {
      targets.delete(target)
    }
    disconnect() {
      targets.clear()
    }
  }
  const fire = () => {
    if (callback) callback([])
  }
  observerRef = { fire }
  for (const scope of [globalThis, globalThis.window]) {
    if (!scope) continue
    Object.defineProperty(scope, 'ResizeObserver', {
      value: Observer,
      configurable: true,
      writable: true,
    })
  }
  return fire
}

/** 最近一次 `stubResizeObserver()` 的触发器（供 `setBox` 自动调用）。 */
let observerRef = null

/**
 * 挂载组件，返回一个够用的小 wrapper。
 *
 * 刻意**不**做完整的 `@vue/test-utils` 复刻：只提供这个项目真的会用到的
 * `find` / `findAll` / `text` / `classes` / `attributes` / `trigger` /
 * `emitted` / `unmount`。
 */
export async function mountComponent(component, props = {}, options = {}) {
  // 动态 import：必须在 setupDom() 之后求值
  const { createApp, h, nextTick } = await import('vue')
  const host = document.createElement('div')
  ;(options.attachTo ?? document.body).appendChild(host)

  const emitted = {}
  // 事件监听在 `h()` 里必须写成 `onJump` / `onUpdate:bucket` 这种 prop 名，
  // **不是** `jump` —— 直接传 `{ jump: fn }` 只会被当成一个叫 `jump` 的 prop 丢掉，
  // 于是 `emitted()` 永远是 undefined（实测踩到）。
  const listeners = {}
  for (const event of options.emits ?? []) {
    const propName = `on${event.charAt(0).toUpperCase()}${event.slice(1)}`
    listeners[propName] = (payload) => {
      ;(emitted[event] ??= []).push([payload])
    }
  }

  const app = createApp({ render: () => h(component, { ...props, ...listeners }) })
  try {
    app.mount(host)
  } catch (error) {
    throw new Error(`挂载失败：${error?.message ?? error}`)
  }
  await nextTick()

  return new Wrapper(host, emitted, app, nextTick)
}

/** 极简 wrapper：只包住一组已渲染的 DOM 节点。 */
class Wrapper {
  constructor(root, emitted, app, nextTick) {
    this.root = root
    this.emittedMap = emitted
    this.app = app
    this.nextTick = nextTick
  }

  /** 渲染出的全部文本（已折叠空白，便于直接断言文案）。 */
  text() {
    return (this.root.textContent ?? '').replace(/\s+/g, ' ')
  }

  find(selector) {
    const element = this.root.querySelector(selector)
    if (!element) throw new Error(`find(${selector}) 没找到元素`)
    return new Wrapper(element, this.emittedMap, this.app, this.nextTick)
  }

  /** 找不到时返回 null 的版本（断言"不该存在"时用）。 */
  maybeFind(selector) {
    const element = this.root.querySelector(selector)
    return element ? new Wrapper(element, this.emittedMap, this.app, this.nextTick) : null
  }

  findAll(selector) {
    return [...this.root.querySelectorAll(selector)].map(
      (element) => new Wrapper(element, this.emittedMap, this.app, this.nextTick),
    )
  }

  get element() {
    return this.root
  }

  classes() {
    return [...(this.root.classList ?? [])]
  }

  attributes(name) {
    if (this.root.getAttribute) {
      const value = this.root.getAttribute(name)
      if (value != null) return value
    }
    // 没有这个属性时退回 property —— 组件的 `:title="''"` 会渲染成
    // 有属性但值为空串，这两种情况在测试里都当"空"处理
    const value = this.root[name]
    return value == null ? undefined : String(value)
  }

  /** 触发事件（真的 `dispatchEvent`，不是调用 handler）。 */
  async trigger(event, init = {}) {
    const Constructor =
      event === 'click' ? window.MouseEvent
        : event === 'wheel' ? window.WheelEvent
          : event === 'mousemove' ? window.MouseEvent
            : event === 'mouseleave' ? window.MouseEvent
              : window.Event
    const instance = new Constructor(event, { bubbles: true, cancelable: true, ...init })
    this.root.dispatchEvent(instance)
    await this.nextTick()
    return instance
  }

  /** 某个事件 emit 过的载荷数组。 */
  emitted(event) {
    return this.emittedMap[event]
  }

  unmount() {
    this.app.unmount()
    this.root.remove?.()
  }
}

/**
 * 造一个"够用"的 2D 上下文替身。
 *
 * 为什么需要：jsdom 没装 node-canvas，`getContext('2d')` 返回 `null`。
 * 那条**降级路径**本身也要测（组件必须不崩），但要验证"到底画了什么"就得有个
 * 记账的替身。两个都要，所以这个函数是可选的、按需注入。
 *
 * 只实现组件真正调用的那几个方法 —— 多写会让替身与真实 canvas 的差异变大，
 * 反而给出虚假的安全感。
 */
export function fakeContext() {
  const calls = { setTransform: 0, clearRect: 0, fillRect: 0 }
  const ops = []
  return {
    calls,
    ops,
    fillStyle: '',
    setTransform(...args) {
      calls.setTransform += 1
      ops.push({ op: 'setTransform', args })
    },
    clearRect(...args) {
      calls.clearRect += 1
      ops.push({ op: 'clearRect', args })
    },
    fillRect(x, y, w, h) {
      calls.fillRect += 1
      // 记下颜色：柱子的高亮是靠 fillStyle 区分的，只数次数看不出画错没有
      ops.push({ op: 'fillRect', x, y, w, h, fill: this.fillStyle })
    },
  }
}

let originalGetContext = null

/**
 * 让**全文档**的 `HTMLCanvasElement` 都用同一个 2D 替身，并把它返回。
 *
 * 每次调用都**换一个新的替身实例**（旧的就别再断言了），并装到 `prototype` 上。
 *
 * 为什么必须装到 prototype 上：组件在 `onMounted` 里就会画一次，那时测试还拿不到
 * canvas 元素（DOM 刚建出来）；而 jsdom 的 `getContext('2d')` 默认返回 `null`，
 * 组件会走"拿不到上下文就不画"的降级分支 —— 于是后面注入替身也**看不到**已经
 * 发生的那次绘制，断言全是空的（实测踩到）。
 */
export function stubCanvasPrototype(ctx = fakeContext()) {
  const proto = globalThis.HTMLCanvasElement?.prototype
  if (!proto) throw new Error('HTMLCanvasElement 还没就绪 —— setupDom() 跑了吗？')
  if (originalGetContext == null) {
    originalGetContext = Object.getOwnPropertyDescriptor(proto, 'getContext') ?? null
  }
  Object.defineProperty(proto, 'getContext', {
    value(kind) {
      return kind === '2d' ? ctx : null
    },
    configurable: true,
    writable: true,
  })
  return ctx
}

/** 撤掉 prototype 上的替身，恢复 jsdom 的行为（`getContext('2d') === null`）。 */
export function restoreCanvasPrototype() {
  const proto = globalThis.HTMLCanvasElement?.prototype
  if (!proto) return
  if (originalGetContext) {
    Object.defineProperty(proto, 'getContext', originalGetContext)
  } else {
    delete proto.getContext
  }
}

/** 把单个 canvas 的 `getContext('2d')` 换成替身（返回替身便于断言）。 */
export function stubCanvasContext(canvas, ctx = fakeContext()) {
  canvas.getContext = (kind) => (kind === '2d' ? ctx : null)
  return ctx
}

/**
 * 把元素量出来的尺寸"按下去"，并**自动**让 `ResizeObserver` 回调一次。
 *
 * jsdom 不做布局，`clientWidth`/`scrollWidth` 一律是 0 —— 而组件在宽度为 0 时
 * **故意不画**（见 `slotGeometry`）。不设这个的话所有绘制断言都会"通过"
 * （因为什么都没画），那种测试等于没写。
 */
export function setBox(element, { clientWidth, clientHeight, scrollWidth, scrollHeight } = {}) {
  const define = (name, value) => {
    if (value == null) return
    Object.defineProperty(element, name, { value, configurable: true, writable: true })
  }
  define('clientWidth', clientWidth)
  define('clientHeight', clientHeight)
  define('scrollWidth', scrollWidth)
  define('scrollHeight', scrollHeight)
  // `getBoundingClientRect()` 在 jsdom 里全是 0 —— 命中测试要拿它算鼠标位置
  const width = clientWidth ?? element.clientWidth ?? 0
  const height = clientHeight ?? element.clientHeight ?? 0
  element.getBoundingClientRect = () => ({
    x: 0,
    y: 0,
    left: 0,
    top: 0,
    right: width,
    bottom: height,
    width,
    height,
    toJSON() {},
  })
  // 尺寸变了要通知观察者：组件只在回调里重新量宽度，不通知就永远认为宽度是 0
  observerRef?.fire()
  return element
}

/**
 * 造一个"喂给组件"的 `/api/density` 响应。
 *
 * 与后端真实形状保持一致（`ts` / `count` / `by_type` / `bucket_s` / `range` / `total`）——
 * 形状对不上时测试会"过"，但面板会白屏，那种测试比没有更糟。
 */
export function makeDensity({
  count = 10,
  bucketS = 300,
  start = 1_789_710_000,
  counts = null,
} = {}) {
  const list = counts ?? Array.from({ length: count }, (_, i) => (i % 3 === 0 ? 0 : i + 1))
  return {
    bucket_s: bucketS,
    range: { lo: start, hi: start + Math.max(list.length - 1, 0) * bucketS },
    total: list.reduce((sum, value) => sum + value, 0),
    buckets: list.map((value, index) => ({
      ts: start + index * bucketS,
      count: value,
      by_type: value ? { danmaku: value } : {},
    })),
  }
}
