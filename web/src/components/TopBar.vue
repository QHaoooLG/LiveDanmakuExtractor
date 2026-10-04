<script setup>
/**
 * 顶栏：**跨面板的过滤与操作**。
 *
 * 排版原则（需求点 6）：顶栏只放"影响整个面板、且随时要够得着"的东西 ——
 * 连接状态、类型过滤、关键词、SC 阈值、计数、导出、停止面板。
 * **房间号的输入/添加/切换都搬到了右侧栏顶部**（那里才是房间的地盘），
 * 所以顶栏不再有房间下拉与输入框，横向拥挤感明显下降。
 *
 * 顶栏右侧按"信息 → 动作"分组：计数/运行时间是只读信息，导出/停止是动作，
 * 中间用分隔线切开，避免误点。
 */
import { ref } from 'vue'
import { formatDuration } from '../format.js'
import TypeSelector from './TypeSelector.vue'

const props = defineProps({
  connected: { type: Boolean, default: false },
  received: { type: Number, default: 0 },
  version: { type: String, default: '' },
  /** 已运行秒数（来自 /api/health） */
  uptime: { type: Number, default: 0 },
  /** 当前启用的类型（数组） */
  enabledTypes: { type: Array, default: () => [] },
  /** 类型下拉的可选项 */
  typeOptions: { type: Array, default: () => [] },
  /** `{ type: 条数 }`：把条数显示在类型选项里 */
  typeCounts: { type: Object, default: () => ({}) },
  keyword: { type: String, default: '' },
  /** 'highlight' | 'filter' */
  keywordMode: { type: String, default: 'highlight' },
  scThreshold: { type: Number, default: 30 },
  busy: { type: Boolean, default: false },
  /** 正在导出统计 CSV（导出要走一次 fetch，期间把按钮置灰防连点） */
  exportingStats: { type: Boolean, default: false },
})

const emit = defineEmits([
  'export',
  'export-stats',
  'update:enabled-types',
  'update:keyword',
  'update:keyword-mode',
  'update:sc-threshold',
  'stop-panel',
])

const confirmingStop = ref(false)

function onThreshold(event) {
  const value = Number(event.target.value)
  emit('update:sc-threshold', Number.isFinite(value) && value >= 0 ? value : 0)
}

function confirmStop() {
  confirmingStop.value = false
  emit('stop-panel')
}
</script>

<template>
  <header class="room-bar">
    <span class="brand">bili-danmaku<small v-if="version">v{{ version }}</small></span>

    <span class="tag" :class="connected ? 'accent' : ''">
      <span class="dot" :class="connected ? 'on' : 'off'"></span>
      {{ connected ? '已连接' : '断开重连中' }}
    </span>

    <TypeSelector
      :enabled-types="enabledTypes"
      :options="typeOptions"
      :counts="typeCounts"
      @update:enabled-types="emit('update:enabled-types', $event)"
    />

    <!-- 关键词：高亮 or 过滤 -->
    <span class="field">
      <select
        class="mode-select"
        :value="keywordMode"
        title="关键词是只高亮，还是只显示命中的弹幕"
        @change="emit('update:keyword-mode', $event.target.value)"
      >
        <option value="highlight">高亮</option>
        <option value="filter">过滤</option>
      </select>
      <input
        :value="keyword"
        type="text"
        :class="{ filtering: keywordMode === 'filter' && keyword }"
        :placeholder="keywordMode === 'filter' ? '只显示含该词的弹幕' : '关键词，仅高亮'"
        style="width:170px"
        @input="emit('update:keyword', $event.target.value)"
      />
      <button v-if="keyword" class="mini" title="清空关键词" @click="emit('update:keyword', '')">
        ×
      </button>
    </span>

    <span class="field">
      <span class="field-label" title="达到该金额的 SC 会在流上方弹横幅；0 表示每条都提示">SC≥</span>
      <input
        :value="scThreshold"
        type="number"
        min="0"
        step="10"
        style="width:56px"
        @input="onThreshold"
      />
    </span>

    <span class="spacer"></span>

    <!-- 只读信息：本面板这一次运行的状态 -->
    <span class="tag" title="本次打开面板后收到的事件条数">已接收 {{ received }}</span>
    <span class="uptime" :title="`面板已连续运行 ${Math.round(uptime)} 秒`">
      <span class="pulse"></span>
      已运行 {{ formatDuration(uptime) }}
    </span>

    <span class="divider"></span>

    <button title="把当前筛选范围导出为 CSV（Excel 直接打开不乱码）" @click="emit('export', 'csv')">
      导出 CSV
    </button>

    <!-- 统计 CSV：与上面的"原始事件导出"是两件事 ——
         这个出的是**聚合报告**（概要 / 类型分布 / 每时段弹幕量 / 发言 TOP，
         同一个文件里多张表），时段长度跟随时间轴当前选择，默认 5 分钟。
         文件名带主播名与当天导出编号，所以由前端另存（见 App.vue::exportStats）。 -->
    <button
      title="导出统计 CSV：弹幕统计 + 每时段弹幕量（同一文件多张表；时段长度跟随时间轴）"
      :disabled="exportingStats"
      @click="emit('export-stats')"
    >
      {{ exportingStats ? '导出中…' : '导出统计' }}
    </button>

    <!-- 停止面板：两步确认，避免误点 -->
    <template v-if="!confirmingStop">
      <button class="danger" title="停止采集并退出面板（会先把数据 flush 落盘）" @click="confirmingStop = true">
        停止面板
      </button>
    </template>
    <template v-else>
      <button class="danger strong" :disabled="busy" @click="confirmStop">
        {{ busy ? '正在停止…' : '确认停止？' }}
      </button>
      <button class="mini" :disabled="busy" @click="confirmingStop = false">取消</button>
    </template>
  </header>
</template>

<style scoped>
.field { display: inline-flex; align-items: center; gap: 4px; }
.field-label { color: var(--muted); font-size: 12px; }
.mode-select {
  background: #0f1115; color: inherit; border: 1px solid var(--border);
  border-radius: 6px; padding: 4px 6px;
}
.mini { padding: 3px 8px; font-size: 12px; }
input.filtering {
  border-color: #f472b6;
  box-shadow: 0 0 0 1px rgba(244, 114, 182, 0.35);
}
.divider { width: 1px; align-self: stretch; background: var(--border); margin: 0 2px; }
.uptime {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 12px; color: #9ecbf5;
  border: 1px solid #2c3a4a; border-radius: 999px; padding: 3px 10px 3px 8px;
  font-variant-numeric: tabular-nums;
}
.pulse {
  width: 8px; height: 8px; border-radius: 50%;
  background: #34d399;
  box-shadow: 0 0 0 0 rgba(52, 211, 153, 0.7);
  animation: breathe 2.4s ease-in-out infinite;
}
@keyframes breathe {
  0%   { box-shadow: 0 0 0 0 rgba(52, 211, 153, 0.55); opacity: 1; }
  50%  { box-shadow: 0 0 0 6px rgba(52, 211, 153, 0); opacity: 0.55; }
  100% { box-shadow: 0 0 0 0 rgba(52, 211, 153, 0); opacity: 1; }
}
@media (prefers-reduced-motion: reduce) {
  .pulse { animation: none; }
}
button.danger { border-color: #7f1d1d; color: #fecaca; }
button.danger.strong { background: #7f1d1d; color: #fff; font-weight: 600; }
</style>
