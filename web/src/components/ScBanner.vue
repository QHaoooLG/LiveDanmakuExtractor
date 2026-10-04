<script setup>
/**
 * 高额 SC 横幅（V2-b 的"告警"在面板侧的落地）。
 *
 * 为什么做成横幅而不是弹窗/声音：面板是**旁挂**的观察工具，
 * 弹窗会打断操作、声音在办公室里很尴尬；横幅足够醒目又能自动消失。
 *
 * 只显示达到阈值的 SC，多个同时到达时排队 —— 队列上限 3 条，
 * 超了丢最旧的（和 hub 的慢消费者策略一致：宁可少显示，不反向拖累采集）。
 */
import { computed, onUnmounted, ref, watch } from 'vue'

const props = defineProps({
  events: { type: Array, required: true },
  /** 金额门槛（元）；SC 的 price 本身就是元 */
  threshold: { type: Number, default: 30 },
  /** 每条展示时长（毫秒） */
  duration: { type: Number, default: 6000 },
})

const queue = ref([])
const seen = new Set()
const MAX_QUEUE = 3
let timer = null

const current = computed(() => queue.value[0] ?? null)

function push(event) {
  if (queue.value.length >= MAX_QUEUE) queue.value.shift()
  queue.value = [...queue.value, event]
  schedule()
}

function schedule() {
  if (timer || !queue.value.length) return
  timer = setTimeout(() => {
    timer = null
    queue.value = queue.value.slice(1)
    schedule()
  }, props.duration)
}

function dismiss() {
  clearTimeout(timer)
  timer = null
  queue.value = queue.value.slice(1)
  schedule()
}

/**
 * 用 `session_id-seq` 做去重键：WS 重连会回填最近事件，
 * 不去重的话同一条 SC 会重复弹。
 */
watch(
  () => props.events,
  (list) => {
    if (!list?.length) return
    for (const event of list) {
      if (event.type !== 'super_chat') continue
      if ((event.price ?? 0) < props.threshold) continue
      const key = `${event.session_id ?? ''}-${event.seq ?? ''}-${event.msg_id ?? ''}`
      if (seen.has(key)) continue
      seen.add(key)
      push(event)
    }
    if (seen.size > 500) seen.clear()
  },
  { deep: false },
)

watch(
  () => props.threshold,
  () => {
    // 换阈值后重新开始判定，避免旧 key 挡住新命中
    seen.clear()
  },
)

onUnmounted(() => clearTimeout(timer))

defineExpose({ current, queue })
</script>

<template>
  <transition name="sc-fade">
    <div v-if="current" class="sc-banner">
      <span class="sc-price">SC {{ current.price ?? '?' }} 元</span>
      <span class="sc-name">{{ current.uname || '匿名' }}</span>
      <span class="sc-text">{{ current.text || '' }}</span>
      <span v-if="queue.length > 1" class="sc-more">还有 {{ queue.length - 1 }} 条</span>
      <button class="sc-close" title="关闭" @click="dismiss">×</button>
    </div>
  </transition>
</template>

<style scoped>
.sc-banner {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin: 0 0 8px;
  padding: 10px 44px 10px 14px;
  position: relative;
  border-radius: 8px;
  border: 1px solid #7f1d4d;
  background: linear-gradient(90deg, #3b1029 0%, #2a1622 100%);
  box-shadow: 0 4px 18px rgba(244, 114, 182, 0.18);
}
.sc-price {
  font-weight: 700;
  color: #f472b6;
  font-variant-numeric: tabular-nums;
  flex-shrink: 0;
}
.sc-name { color: #9ecbf5; flex-shrink: 0; max-width: 160px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sc-text { color: #f5e6ef; word-break: break-word; }
.sc-more { color: #8b93a1; font-size: 12px; margin-left: auto; flex-shrink: 0; }
.sc-close {
  position: absolute; right: 8px; top: 6px;
  background: none; border: none; color: #8b93a1;
  font-size: 16px; cursor: pointer; padding: 2px 6px;
}
.sc-close:hover { color: #e6e6e6; }
.sc-fade-enter-active, .sc-fade-leave-active { transition: opacity .18s ease, transform .18s ease; }
.sc-fade-enter-from, .sc-fade-leave-to { opacity: 0; transform: translateY(-6px); }
</style>
