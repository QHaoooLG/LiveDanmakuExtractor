<script setup>
/**
 * 顶栏：**跨面板的过滤与操作**。
 *
 * 排版原则（需求点 6）：顶栏只放"影响整个面板、且随时要够得着"的东西 ——
 * 连接状态、类型过滤、关键词、计数、导出、停止面板。
 * **房间号的输入/添加/切换都搬到了右侧栏顶部**（那里才是房间的地盘），
 * 所以顶栏不再有房间下拉与输入框，横向拥挤感明显下降。
 *
 * 顶栏右侧按"信息 → 动作"分组：计数/运行时间是只读信息，导出/停止是动作，
 * 中间用分隔线切开，避免误点。
 *
 * > **SC 金额门槛已移除**（需求 2026-10-07 #3/#4）：原来的 `SC≥` 输入框连带
 * > `scThreshold` prop / `update:sc-threshold` 事件一起去掉，`ScBanner` 也不再
 * > 按金额过滤 —— 收到 SC 就提示。后端 `filters.min_price` 与 CLI `--min-price`
 * > 同样删除，全项目不再有"按金额筛事件"这个概念。
 */
import { computed, ref } from 'vue'
import { formatDuration } from '../format.js'
import { attentionBadge as attentionBadgeFor } from './health.js'
import TypeSelector from './TypeSelector.vue'

const props = defineProps({
  connected: { type: Boolean, default: false },
  received: { type: Number, default: 0 },
  version: { type: String, default: '' },
  /** 已运行秒数（来自 /api/health） */
  uptime: { type: Number, default: 0 },
  /**
   * 服务端累计收到的事件条数（`/api/stats` 的 `cumulative.total`）。
   *
   * 用来和浏览器侧的「已接收」对照 —— 两个数字口径不同，差值本身就是信息
   * （差得多说明页面刷新过、或者 WebSocket 断过）。
   */
  serverTotal: { type: Number, default: 0 },
  /** 当前启用的类型（数组） */
  enabledTypes: { type: Array, default: () => [] },
  /** 类型下拉的可选项 */
  typeOptions: { type: Array, default: () => [] },
  /** `{ type: 条数 }`：把条数显示在类型选项里 */
  typeCounts: { type: Object, default: () => ({}) },
  keyword: { type: String, default: '' },
  /** 'highlight' | 'filter' */
  keywordMode: { type: String, default: 'highlight' },
  busy: { type: Boolean, default: false },
  /** 正在导出统计数据（导出要走一次 fetch，期间把按钮置灰防连点） */
  exportingStats: { type: Boolean, default: false },
  /**
   * `/api/health` 的 `attention`（`analysis.assess_health()` 的结果）。
   *
   * 需求："把凭证失效/重连异常的健康横幅做得**更显眼**"。
   * 横幅可以被关掉，而"关掉之后又忘了"正是这类提示最典型的失效方式 ——
   * 所以这里再挂一个**常驻角标**：只要 attention 不是 ok，它就一直在（不可关），
   * 点它可以重新展开横幅。
   */
  attention: { type: Object, default: null },
})

const emit = defineEmits([
  'export',
  'export-stats',
  'update:enabled-types',
  'update:keyword',
  'update:keyword-mode',
  'stop-panel',
  /** 点常驻角标：让 App 把健康横幅重新展开（用户可能之前关过它） */
  'show-attention',
])

const confirmingStop = ref(false)

/**
 * 常驻的健康角标内容（`null` = 一切正常，不显示）。
 *
 * 为什么要有它 —— 横幅是**可关闭**的，而"关掉之后又忘了"正是这类提示最典型的
 * 失效方式。角标不可关：只要有问题，它就一直在顶栏上。
 * 判定与文案在 `health.js`（纯函数，可离线单测）。
 */
const attentionBadge = computed(() => attentionBadgeFor(props.attention))

/**
 * 「已接收」的 tooltip：把口径与**服务端口径**一起说清楚。
 *
 * 两个数字不一样是正常的，但必须让用户能自己看出差别在哪 —— 否则
 * "我明明采了两小时，怎么才 300 条"这种疑问会一直存在。
 */
const receivedTitle = computed(() => {
  const parts = [
    '浏览器这一侧收到的事件条数（只统计当前房间）',
    '· 刷新页面会归零，但服务端还在数',
    '· WebSocket 断线期间的事件收不到（重连后不归零）',
    `· 服务端本次运行共收到 ${props.serverTotal} 条（含所有房间）`,
  ]
  return parts.join('\n')
})

function confirmStop() {
  confirmingStop.value = false
  emit('stop-panel')
}

/**
 * 「取消」：清空关键词**并**回到「高亮」模式。
 *
 * 为什么两件事一起做：只清关键词而留在过滤态，用户看到的是"过滤条件还在生效、
 * 但输入框空了" —— 那时 `visible` 已经退回全量（过滤靠关键词），
 * 状态与观感对不上。语义上"取消过滤"本来就是这个意思。
 */
function cancelFilter() {
  emit('update:keyword', '')
  emit('update:keyword-mode', 'highlight')
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

    <!-- 关键词：高亮 or 过滤。
         「过滤 / 取消」两个按钮**常驻**在输入框右侧（需求 2026-10-07 #2）：
         以前"清空"是个 `×`，只在有关键词时才出现，用户得先猜它会不会在；
         现在两个动作位置固定、一眼可见 —— "能过滤、也能取消"。 -->
    <span class="field">
      <input
        :value="keyword"
        type="text"
        :class="{ filtering: keywordMode === 'filter' && keyword }"
        :placeholder="keywordMode === 'filter' ? '只显示含该词的弹幕' : '关键词，仅高亮'"
        style="width:170px"
        @input="emit('update:keyword', $event.target.value)"
      />
      <button
        class="mini"
        :class="{ on: keywordMode === 'filter' }"
        :disabled="!keyword"
        :title="keyword ? '只显示含该关键词的弹幕' : '先输入关键词再过滤'"
        @click="emit('update:keyword-mode', 'filter')"
      >
        过滤
      </button>
      <button
        class="mini"
        :disabled="!keyword"
        title="清空关键词并回到「高亮」模式"
        @click="cancelFilter"
      >
        取消
      </button>
    </span>

    <!-- 常驻健康角标：问题没解决就一直亮着（横幅可以被关掉，它不能） -->
    <button
      v-if="attentionBadge"
      class="attention-badge"
      :class="attentionBadge.level"
      :title="attentionBadge.title"
      @click="emit('show-attention')"
    >
      {{ attentionBadge.text }}
    </button>

    <span class="spacer"></span>

    <!-- 只读信息：本面板这一次运行的状态。
         「已接收」是**浏览器这一侧**收到的事件条数（`useLiveStream` 的 `received`），
         所以必须把口径写进文字里 —— 它和右边的「已运行」（服务端口径）不是一回事：
           * 刷新页面就归零（服务端还在数）；
           * WebSocket 重连后不归零，但断线期间的事件收不到；
           * 只统计**当前房间**的事件。
         tooltip 里给出服务端的 `cumulative.total`，两者差多少一眼能看出来。 -->
    <span class="tag" :title="receivedTitle">已接收 {{ received }}（本次打开页面）</span>
    <span class="uptime" :title="`面板已连续运行 ${Math.round(uptime)} 秒`">
      <span class="pulse"></span>
      已运行 {{ formatDuration(uptime) }}
    </span>

    <span class="divider"></span>

    <button title="把当前筛选范围导出为 CSV（Excel 直接打开不乱码）" @click="emit('export', 'csv')">
      导出 CSV
    </button>

    <!-- 统计数据导出：与上面的"原始事件导出"是两件事 ——
         这个出的是**聚合报告**（概要 / 类型分布 / 每时段弹幕量 / 发言 TOP），
         优先给 xlsx（一张表一个 sheet），后端没装 openpyxl 时静默退回 CSV；
         时段长度跟随时间轴当前选择，默认 5 分钟。
         文件名带主播名与当天导出编号，所以由前端另存（见 App.vue::exportStats）。 -->
    <button
      title="导出统计数据：概要 / 类型分布 / 每时段弹幕量 / 发言 TOP（优先 xlsx，多张表分 sheet；时段长度跟随时间轴）"
      :disabled="exportingStats"
      @click="emit('export-stats')"
    >
      {{ exportingStats ? '导出中…' : '导出统计' }}
    </button>

    <!-- 停止面板：两步确认，避免误点。
         若当前有**认证类**问题，确认按钮上再补一句 —— 那种情况下停掉采集
         等于"在没修好之前就放弃了这段数据"，值得多提醒一次。 -->
    <template v-if="!confirmingStop">
      <button class="danger" title="停止采集并退出面板（会先把数据 flush 落盘）" @click="confirmingStop = true">
        停止面板
      </button>
    </template>
    <template v-else>
      <button
        class="danger strong"
        :disabled="busy"
        :title="attentionBadge && attentionBadge.level === 'alert'
          ? '注意：当前登录已失效，采集本来就没在正常收数据；确认要现在停止吗？'
          : '确认停止采集并退出面板'"
        @click="confirmStop"
      >
        {{ busy ? '正在停止…' : attentionBadge && attentionBadge.level === 'alert' ? '确认停止？（登录已失效）' : '确认停止？' }}
      </button>
      <button class="mini" :disabled="busy" @click="confirmingStop = false">取消</button>
    </template>
  </header>
</template>

<style scoped>
.field { display: inline-flex; align-items: center; gap: 4px; }
/* 常驻健康角标：红/黄两档，故意做得比普通 tag 重（边框 + 更亮的字色） */
.attention-badge {
  padding: 1px 9px; border-radius: 999px; font-size: 12px; cursor: pointer;
  background: none; border: 1px solid currentColor; font-weight: 600;
  animation: attention-pulse 1.8s ease-in-out infinite;
}
.attention-badge.warn { color: #e3b341; }
.attention-badge.alert { color: #f85149; }
.attention-badge:hover { background: rgba(255, 255, 255, 0.08); }
/* 轻微呼吸：不做花哨动效，只要"余光能注意到"就够 */
@keyframes attention-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.62; }
}
/* 尊重"减少动效"偏好（前庭功能障碍的用户会被闪烁困扰） */
@media (prefers-reduced-motion: reduce) {
  .attention-badge { animation: none; }
}
.field-label { color: var(--muted); font-size: 12px; }
.mini { padding: 3px 8px; font-size: 12px; }
/* 「过滤」处于生效态时给个明显的选中外观（与时间轴的格宽档位一致） */
.mini.on { color: var(--bg); background: var(--accent); border-color: var(--accent); font-weight: 600; }
.mini:disabled { opacity: .45; cursor: not-allowed; }
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
