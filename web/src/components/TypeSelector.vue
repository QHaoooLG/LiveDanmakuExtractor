<script setup>
/**
 * 类型过滤控件：**自绘下拉 + 复选框列表**。
 *
 * 为什么换掉原来的原生 `<select multiple size="1">`（需求点 5：可读性欠缺）：
 *   * 原生多选下拉在**收起状态看不到已选了哪些**，只显示一行被截断的文字；
 *   * 每种类型没有颜色标识，和弹幕流里的高亮对不上；
 *   * "已选 3/10"这种状态也没法表达。
 *
 * 代价：不再白送键盘可达性与"点外部收起"。这两件事在这里补回来：
 *   * 触发按钮是一个真 `<button>`（可 Tab 聚焦、Enter/Space 打开），
 *     打开后每个条目是原生 `<input type="checkbox">`（Tab 可达、屏幕阅读器可读）；
 *   * `document` 上挂一次性 `pointerdown` 监听实现点外部收起，`Escape` 关闭。
 *
 * 收起态显示的是**摘要 + 已选类型色块**——这正是"可读性"要解决的问题。
 */
import { computed, onUnmounted, ref, watch } from 'vue'
import { typeColor, typeLabel } from '../format.js'

const props = defineProps({
  /** 当前启用的类型数组；长度 >= 全部选项表示不过滤 */
  enabledTypes: { type: Array, default: () => [] },
  /** 可选项（已知顺序 + 流里出现过的未知类型） */
  options: { type: Array, default: () => [] },
  /** `{ type: 条数 }`：把条数写在选项后面，帮助判断哪些类型值得看 */
  counts: { type: Object, default: () => ({}) },
})

const emit = defineEmits(['update:enabledTypes'])

const open = ref(false)
const root = ref(null)

const selected = computed(() => new Set(props.enabledTypes))
const allSelected = computed(
  () => props.options.length > 0 && props.options.every((item) => selected.value.has(item.value)),
)

/** 收起态的摘要文案。 */
const summary = computed(() => {
  const total = props.options.length
  const picked = props.options.filter((item) => selected.value.has(item.value))
  if (!total) return '无可用类型'
  if (picked.length === 0) return '全部隐藏'
  if (picked.length === total) return `全部 ${total} 种`
  if (picked.length === 1) return picked[0].label
  return `${picked.length}/${total} 种`
})

/** 摘要里最多铺 3 个色块，再多就只是噪音。 */
const badges = computed(() =>
  props.options.filter((item) => selected.value.has(item.value)).slice(0, 3),
)

function toggle(type) {
  const next = new Set(selected.value)
  if (next.has(type)) next.delete(type)
  else next.add(type)
  emit('update:enabledTypes', props.options.map((item) => item.value).filter((value) => next.has(value)))
}

function selectAll() {
  emit('update:enabledTypes', props.options.map((item) => item.value))
}

function clearAll() {
  emit('update:enabledTypes', [])
}

function onDocumentPointerDown(event) {
  if (!open.value) return
  if (root.value && !root.value.contains(event.target)) open.value = false
}

function onKeydown(event) {
  if (event.key === 'Escape') open.value = false
}

watch(open, (value) => {
  if (value) {
    document.addEventListener('pointerdown', onDocumentPointerDown)
    document.addEventListener('keydown', onKeydown)
  } else {
    document.removeEventListener('pointerdown', onDocumentPointerDown)
    document.removeEventListener('keydown', onKeydown)
  }
})

onUnmounted(() => {
  document.removeEventListener('pointerdown', onDocumentPointerDown)
  document.removeEventListener('keydown', onKeydown)
})
</script>

<template>
  <div ref="root" class="type-filter">
    <span class="field-label">类型</span>
    <button
      class="trigger"
      :class="{ open, empty: !enabledTypes.length }"
      type="button"
      :aria-expanded="open"
      title="选择要显示在弹幕流里的类型"
      @click="open = !open"
    >
      <span class="badges">
        <span
          v-for="item in badges"
          :key="item.value"
          class="swatch"
          :style="{ background: typeColor(item.value) }"
        ></span>
      </span>
      <span class="summary">{{ summary }}</span>
      <span class="caret">{{ open ? '▲' : '▼' }}</span>
    </button>

    <div v-if="open" class="panel" role="group" aria-label="弹幕类型过滤">
      <div class="panel-head">
        <button class="mini" type="button" :disabled="allSelected" @click="selectAll">全选</button>
        <button class="mini" type="button" :disabled="!enabledTypes.length" @click="clearAll">
          清空
        </button>
        <span class="spacer"></span>
        <button class="mini" type="button" @click="open = false">收起</button>
      </div>
      <label v-for="item in options" :key="item.value" class="option">
        <input
          type="checkbox"
          :checked="selected.has(item.value)"
          @change="toggle(item.value)"
        />
        <span class="swatch" :style="{ background: typeColor(item.value) }"></span>
        <span class="option-label">{{ item.label }}</span>
        <span class="option-type">{{ item.value }}</span>
        <span class="option-count">{{ counts[item.value] ?? 0 }}</span>
      </label>
      <div v-if="!options.length" class="option-empty">暂无可用类型</div>
    </div>
  </div>
</template>

<style scoped>
.type-filter { position: relative; display: inline-flex; align-items: center; gap: 4px; }
.field-label { color: var(--muted); font-size: 12px; }

.trigger {
  display: inline-flex; align-items: center; gap: 6px;
  min-width: 116px; padding: 4px 8px;
  background: #0f1115; border: 1px solid var(--border); border-radius: 6px;
  font-size: 13px;
}
.trigger.open { border-color: var(--accent); }
.trigger.empty .summary { color: #f9a8d4; }
.badges { display: inline-flex; gap: 2px; }
.swatch {
  width: 9px; height: 9px; border-radius: 2px; flex-shrink: 0;
  box-shadow: inset 0 0 0 1px rgba(0, 0, 0, 0.35);
}
.summary { flex: 1; text-align: left; white-space: nowrap; }
.caret { color: var(--muted); font-size: 9px; }

/* 面板从触发按钮正下方展开；宽度固定，避免被窄视口挤变形 */
.panel {
  position: absolute; top: calc(100% + 6px); left: 0; z-index: 40;
  width: 236px; max-height: 320px; overflow-y: auto;
  background: var(--panel-2); border: 1px solid var(--border); border-radius: 8px;
  box-shadow: 0 10px 28px rgba(0, 0, 0, 0.55);
  padding: 6px;
}
.panel-head { display: flex; align-items: center; gap: 6px; padding: 2px 2px 6px; border-bottom: 1px solid var(--border); margin-bottom: 4px; }
.panel-head .spacer { flex: 1; }
.mini { padding: 2px 8px; font-size: 12px; }
.option {
  display: grid; grid-template-columns: auto auto 1fr auto auto;
  align-items: center; gap: 7px;
  padding: 5px 6px; border-radius: 5px; cursor: pointer; font-size: 13px;
}
.option:hover { background: #2a2f39; }
.option input { accent-color: var(--accent); margin: 0; }
.option-label { white-space: nowrap; }
.option-type { color: #5b6472; font-size: 11px; overflow: hidden; text-overflow: ellipsis; }
.option-count { color: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.option-empty { color: var(--muted); font-size: 12px; padding: 8px 6px; }
</style>
