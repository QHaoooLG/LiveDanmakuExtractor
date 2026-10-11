/**
 * 「正在加载更早的弹幕…」的**防闪烁**门控（需求 2026-10-09 第七轮）。
 *
 * ## 为什么需要它
 *
 * 向上翻一页只要 25~40 ms，而"加载中"提示如果立刻出现又立刻消失，
 * 用户看到的是**闪一下**——比不显示更像"卡了一下"。
 * 所以规则是两条（业界常用的做法）：
 *
 * 1. **延迟显示**：加载超过 `showDelay`（150 ms）才显示 —— 快请求根本不打扰用户；
 * 2. **最短可见**：一旦显示，至少停留 `minVisible`（300 ms）—— 避免"刚出现就没了"。
 *
 * 这两条在**快速连续翻页**时也要成立（连续 `update(true/false)` 不能闪）：
 * 只要还在加载中就不收起计时，只有真的加载完才进入"最短可见"倒计时。
 *
 * 纯逻辑、无 Vue 依赖，时间与定时器都可注入 ⇒ 可以用 `node --test` 直接验。
 */

/** 加载多久才值得显示提示（毫秒）。 */
export const LOADING_SHOW_DELAY_MS = 150
/** 提示一旦显示，至少停留多久（毫秒）。 */
export const LOADING_MIN_VISIBLE_MS = 300

/**
 * @param {object} [options]
 * @param {number} [options.showDelay] 延迟显示阈值
 * @param {number} [options.minVisible] 最短可见时长
 * @param {() => number} [options.now] 取当前时间（测试可注入假时钟）
 * @param {(fn: () => void, ms: number) => any} [options.schedule] 定时器
 * @param {(handle: any) => void} [options.cancel] 清定时器
 * @param {(visible: boolean) => void} [options.onChange] 可见性变化的回调
 */
export function createFlickerGuard({
  showDelay = LOADING_SHOW_DELAY_MS,
  minVisible = LOADING_MIN_VISIBLE_MS,
  now = () => Date.now(),
  schedule = (fn, ms) => setTimeout(fn, ms),
  cancel = (handle) => clearTimeout(handle),
  onChange = () => {},
} = {}) {
  let visible = false
  let shownAt = 0
  let timer = null
  let timerKind = null // 'show' | 'hide' | null

  function fire(next) {
    if (visible === next) return
    visible = next
    if (next) shownAt = now()
    onChange(next)
  }

  function stopTimer() {
    if (timer !== null) {
      cancel(timer)
      timer = null
    }
    timerKind = null
  }

  return {
    /** 当前是否应当显示提示（给测试与调用方读）。 */
    get visible() {
      return visible
    },
    /**
     * 喂入"还在加载吗"。
     *
     * * `update(true)`：**首次**进入加载才起延迟计时；已经在显示/正在计时就什么都不做
     *   （连续翻页不会重置倒计时，用户不会看到闪断）；
     * * `update(false)`：加载结束了。还没显示 ⇒ 直接取消延迟计时（**这一页太快，不打扰**）；
     *   已经显示 ⇒ 补足"最短可见"再收起。
     */
    update(loading) {
      if (loading) {
        // ⚠️ 正在**收起**的倒计时必须被取消：连续翻页时"下一页又开始了"是常态，
        // 不取消的话那支计时器到点仍会把提示收掉（实测：连续翻页会闪断）。
        if (visible) {
          if (timerKind === 'hide') stopTimer()
          return
        }
        if (timer !== null) return
        timerKind = 'show'
        timer = schedule(() => {
          timer = null
          timerKind = null
          fire(true)
        }, showDelay)
        return
      }
      if (!visible) {
        stopTimer()
        return
      }
      const remaining = Math.max(0, minVisible - (now() - shownAt))
      if (remaining === 0) {
        stopTimer()
        fire(false)
        return
      }
      if (timer !== null) return
      timerKind = 'hide'
      timer = schedule(() => {
        timer = null
        timerKind = null
        fire(false)
      }, remaining)
    },
    /** 组件卸载时调用：清掉未触发的计时器（防定时器打到已销毁的组件上）。 */
    dispose() {
      stopTimer()
    },
  }
}
