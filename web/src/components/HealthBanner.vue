<script setup>
/**
 * 采集健康横幅：把"现在需要我做什么"显示在面板上。
 *
 * 需求背景：**运行中**凭据失效时，用户原来只看到「重连中」——
 * 不知道该重新扫码登录（只能自己去翻日志）。启动面板的登录提示条只覆盖了"启动时"，
 * 这里补上"运行中"。
 *
 * 数据来自 `/api/health` 的 `attention`（后端 `analysis.assess_health()` 算好的）。
 * 文案与分级**全部由后端给**，前端只负责渲染 —— 判断逻辑只有一份，
 * 不会出现"后端说要重新登录、前端说是网络问题"这种分裂。
 *
 * 关闭语义：横幅是**持续状态**而不是一次性通知，所以关闭是按"这条具体问题"记的；
 * 换了一个问题（标题/房间变了）会重新出现。问题自己消失后（例如重新登录成功）
 * 记忆也会清掉，不会把下一次也一起吞掉。
 */
import { computed, ref, watch } from 'vue'

const props = defineProps({
  /** `/api/health` 的 `attention`：`{level, title, detail, rooms}` */
  attention: { type: Object, default: null },
})

const emit = defineEmits(['open-login'])

const dismissed = ref('')

const visible = computed(() => {
  const payload = props.attention
  if (!payload || payload.level === 'ok' || !payload.title) return null
  return payload
})

/** 这条问题的稳定标识：换问题就该重新提示，同一个问题关掉后就别再弹。 */
const key = computed(() => {
  const payload = visible.value
  if (!payload) return ''
  return `${payload.level}|${payload.title}|${(payload.rooms ?? []).join(',')}`
})

// 问题消失（level 回到 ok）时清掉关闭记忆：
// 否则"关掉过网络提示 → 后来又出了凭证失效"会因为 key 碰巧相同而被吞掉。
watch(
  () => props.attention?.level,
  (level) => {
    if (!level || level === 'ok') dismissed.value = ''
  },
)

const showBanner = computed(() => Boolean(visible.value) && key.value !== dismissed.value)

/** 凭证失效时直接给一个入口（启动面板里能扫码登录）。 */
const needsRelogin = computed(
  () => visible.value?.level === 'alert' && /登录|凭证|认证/.test(visible.value.title),
)

function dismiss() {
  dismissed.value = key.value
}
</script>

<template>
  <transition name="health-fade">
    <div v-if="showBanner" class="health-banner" :class="visible.level">
      <span class="health-title">{{ visible.title }}</span>
      <span class="health-detail">{{ visible.detail }}</span>
      <button
        v-if="needsRelogin"
        class="health-action"
        title="打开启动面板扫码登录（凭据失效时这一步就能修好）"
        @click="emit('open-login')"
      >
        去登录
      </button>
      <button class="health-close" title="关闭本提示（下次出现别的问题会再提示）" @click="dismiss">×</button>
    </div>
  </transition>
</template>

<style scoped>
.health-banner {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin: 0 0 8px;
  padding: 9px 44px 9px 14px;
  position: relative;
  border-radius: 8px;
  border: 1px solid #7f5f1d;
  background: linear-gradient(90deg, #2a2411 0%, #1f1c14 100%);
  color: #e3b341;
}
/* 凭证失效是"需要你动手"，必须比警告更醒目 */
.health-banner.alert {
  border-color: #7f1d1d;
  background: linear-gradient(90deg, #2d1416 0%, #21161a 100%);
  color: #f85149;
  box-shadow: 0 4px 18px rgba(248, 81, 73, 0.16);
}
.health-title { font-weight: 700; flex-shrink: 0; }
.health-detail { color: #c9c9c9; word-break: break-word; font-size: 12px; }
.health-action {
  flex-shrink: 0;
  background: none;
  border: 1px solid currentColor;
  color: inherit;
  border-radius: 6px;
  padding: 1px 10px;
  font-size: 12px;
  cursor: pointer;
}
.health-action:hover { background: rgba(255, 255, 255, 0.08); }
.health-close {
  position: absolute; right: 8px; top: 5px;
  background: none; border: none; color: #8b93a1;
  font-size: 16px; cursor: pointer; padding: 2px 6px;
}
.health-close:hover { color: #e6e6e6; }
.health-fade-enter-active, .health-fade-leave-active { transition: opacity .18s ease, transform .18s ease; }
.health-fade-enter-from, .health-fade-leave-to { opacity: 0; transform: translateY(-6px); }
</style>
