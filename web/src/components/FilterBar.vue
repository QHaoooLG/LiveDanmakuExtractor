<script setup>
/**
 * 上下文条：统计口径 + **历史浏览控制** + 关键词过滤状态。
 *
 * 类型过滤与关键词输入都在顶栏（`TopBar.vue`）—— 那里是"影响全局面板"的开关的自然位置；
 * 这一条只做**状态显示与导航**，避免同一件事有两个入口。
 *
 * V3-a 起多了一组"复盘"控件：进入/退出历史、翻页、按抓取会话过滤。
 * 放在这里而不是顶栏，是因为它们是**视图状态**（我在看哪一段），
 * 不是全局开关 —— 与"统计范围"属于同一类。
 */
const props = defineProps({
  /** 'cumulative' | 'window' */
  statsMode: { type: String, default: 'cumulative' },
  /** 最近时段的秒数（statsMode === 'window' 时生效） */
  statsWindow: { type: Number, default: 300 },
  keywordMode: { type: String, default: 'highlight' },
  keyword: { type: String, default: '' },
  /** 过滤后剩余条数 / 缓冲总条数，用于给出反馈 */
  filteredCount: { type: Number, default: 0 },
  totalCount: { type: Number, default: 0 },
  /** 面板启动时间（累计口径的起点，来自 /api/stats） */
  startedAt: { type: Number, default: 0 },
  /** 是否正在浏览历史（复盘模式） */
  browsing: { type: Boolean, default: false },
  /** 历史窗口里的条数 */
  historyCount: { type: Number, default: 0 },
  loadingHistory: { type: Boolean, default: false },
  /** 抓取会话列表（`/api/sessions`） */
  sessions: { type: Array, default: () => [] },
  sessionId: { type: String, default: null },
  /** 是否匿名化弹幕流的发言者昵称（开关在"浏览历史"那一栏的最右侧） */
  anonymizeName: { type: Boolean, default: false },
})

const emit = defineEmits([
  'update:stats-mode',
  'update:stats-window',
  'update:anonymize-name',
  'enter-history',
  'back-to-live',
  'select-session',
])

const WINDOWS = [
  { value: 60, label: '最近 1 分钟' },
  { value: 300, label: '最近 5 分钟' },
  { value: 1800, label: '最近 30 分钟' },
  { value: 3600, label: '最近 1 小时' },
]

function startTime(seconds) {
  if (!seconds) return ''
  const date = new Date(seconds * 1000)
  const pad = (n) => String(n).padStart(2, '0')
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

/** 会话下拉的显示文案：`09-29 23:17 · 房间 804456 · 1234 条` */
function sessionLabel(session) {
  const date = new Date((session.started_at ?? 0) * 1000)
  const pad = (n) => String(n).padStart(2, '0')
  const stamp = `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
  const unfinished = session.ended_at == null ? '（未正常结束）' : ''
  return `${stamp} · 房间 ${session.room_id} · ${session.events} 条${unfinished}`
}
</script>

<template>
  <div class="filter-bar">
    <span style="color:var(--muted);font-size:12px">统计范围</span>
    <select
      :value="statsMode"
      title="统计口径：从面板启动累计，或只看最近一段时间"
      style="background:#0f1115;color:inherit;border:1px solid var(--border);border-radius:6px;padding:4px 6px"
      @change="emit('update:stats-mode', $event.target.value)"
    >
      <option value="cumulative">从启动累计</option>
      <option value="window">最近时段</option>
    </select>

    <select
      v-if="statsMode === 'window'"
      :value="statsWindow"
      title="统计窗口长度"
      style="background:#0f1115;color:inherit;border:1px solid var(--border);border-radius:6px;padding:4px 6px"
      @change="emit('update:stats-window', Number($event.target.value))"
    >
      <option v-for="item in WINDOWS" :key="item.value" :value="item.value">{{ item.label }}</option>
    </select>
    <span v-else-if="startedAt" class="hint">起点 {{ startTime(startedAt) }}</span>

    <span class="divider"></span>

    <!-- 复盘器：进入/退出历史 + 会话过滤。
         **刻意不放"↑更早 / ↓更新"按钮**：实测这两个按钮的语义与用户预期不符
         （手动翻页与窗口游标互相干扰，容易翻出重复或空页），已删除。
         更早的内容改由**弹幕流滚到顶部自动加载**（`StreamList` 的 near-top），
         定位则交给时间轴点选 —— 每条路径都只有一个入口。 -->
    <template v-if="!browsing">
      <button
        class="mini"
        title="向上翻阅历史弹幕（数据来自 SQLite，不受实时缓冲上限限制）"
        @click="emit('enter-history')"
      >
        浏览历史
      </button>
    </template>
    <template v-else>
      <span class="badge history">历史浏览中 · {{ historyCount }} 条</span>
      <span v-if="loadingHistory" class="hint">加载中…</span>
      <span v-else class="hint">滚到顶部可继续加载更早</span>
      <button class="mini accent" title="回到实时弹幕（退出历史浏览）" @click="emit('back-to-live')">
        回到实时
      </button>
    </template>

    <select
      v-if="sessions.length"
      class="session-select"
      :value="sessionId ?? ''"
      title="只看某一次抓取（会话）的数据"
      @change="emit('select-session', $event.target.value || null)"
    >
      <option value="">全部会话</option>
      <option v-for="session in sessions" :key="session.id" :value="session.id">
        {{ sessionLabel(session) }}
      </option>
    </select>

    <span class="divider"></span>

    <span
      v-if="keywordMode === 'filter' && keyword"
      class="badge"
      title="当前只显示含关键词的弹幕；把顶栏关键词模式切回「高亮」即可恢复全部"
    >
      只看「{{ keyword }}」· {{ filteredCount }}/{{ totalCount }}
    </span>
    <span v-else class="hint">关键词是「高亮」模式，想看过滤结果请切到「过滤」</span>

    <span class="spacer"></span>

    <!-- 显示偏好：紧跟在"浏览历史"那一栏的最右侧（`margin-left:auto` 把它推到行尾），
         与左边的"视图状态/导航"分开，避免和过滤状态挤在一起。
         注意这是**匿名化**（只留首字符），不是"隐藏昵称" —— 默认不匿名。 -->
    <label class="toggle" title="匿名显示发言者昵称（只保留第一个字符，其余用 * 掩盖）；录屏/截图时用">
      <input
        type="checkbox"
        :checked="anonymizeName"
        @change="emit('update:anonymize-name', $event.target.checked)"
      />
      匿名昵称
    </label>
  </div>
</template>

<style scoped>
.hint { color: var(--muted); font-size: 12px; }
.divider { width: 1px; align-self: stretch; background: var(--border); margin: 0 2px; }
.mini { padding: 2px 9px; font-size: 12px; }
.mini.accent { color: var(--accent); border-color: #1e4d63; }
.session-select {
  max-width: 250px;
  background: #0f1115; color: inherit; border: 1px solid var(--border);
  border-radius: 6px; padding: 4px 6px; font-size: 12px;
}
.badge {
  font-size: 12px; color: #f9a8d4;
  border: 1px solid #7f1d4d; background: #2a1622;
  border-radius: 999px; padding: 3px 10px;
  font-variant-numeric: tabular-nums;
}
.badge.history { color: #9ecbf5; border-color: #1e4d63; background: #16232c; }
/* 显示偏好开关：与 App.vue 顶栏里那几个 checkbox 一致（accent-color + 小字号） */
.toggle {
  display: inline-flex; align-items: center; gap: 4px;
  color: var(--muted); font-size: 12px; cursor: pointer; user-select: none;
  white-space: nowrap;
}
.toggle:hover { color: var(--text); }
.toggle input { accent-color: var(--accent); margin: 0; }
</style>
