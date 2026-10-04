<script setup>
/**
 * 标记条（V3-a2）：打标记 / 跳到标记 / 按标记区间导出。
 *
 * **为什么放在时间轴下方而不是右栏**：标记本质是"时间轴上的锚点"，
 * 与 `DensityBar` 用同一套时间语义；放在一起时"点格子跳转"和"点标记跳转"
 * 是同一个动作的两种粒度，用户不需要在两处之间来回看。
 *
 * 隐私与措辞：标记是**用户自己的主观产出**，不是判定。
 * 所以文案用"标记/备注"，不提供任何自动化结论；数据只存本机独立库。
 */
import { computed, ref } from 'vue'

const props = defineProps({
  bookmarks: { type: Array, default: () => [] },
  activeId: { type: Number, default: null },
  /** 当前可标记的时刻（最后一条可见弹幕的时间）；没有数据时为 null */
  playheadTs: { type: Number, default: null },
  /** 是否正在浏览历史（决定"标记此刻"的语义提示） */
  browsing: { type: Boolean, default: false },
  busy: { type: Boolean, default: false },
})

const emit = defineEmits(['add', 'jump', 'select', 'remove', 'clear', 'export-range'])

const collapsed = ref(false)
const draftLabel = ref('')

const active = computed(() => props.bookmarks.find((item) => item.id === props.activeId) ?? null)
/** 选中的标记在列表里的位置（导出提示"到下一个标记"要用） */
const activeIndex = computed(() => props.bookmarks.findIndex((item) => item.id === props.activeId))

function stamp(seconds) {
  const date = new Date(seconds * 1000)
  const pad = (n) => String(n).padStart(2, '0')
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(
    date.getMinutes(),
  )}:${pad(date.getSeconds())}`
}

function submit() {
  const recvTs = props.playheadTs
  if (recvTs == null) return
  emit('add', { recvTs, label: draftLabel.value.trim() || null })
  draftLabel.value = ''
}

/** 导出区间：有选中标记时从它出发（终点交给后端取"下一个标记"）。 */
function exportRange(format) {
  if (active.value) emit('export-range', { bookmarkId: active.value.id, format })
}
</script>

<template>
  <div class="bookmark-bar">
    <div class="row">
      <span class="title">标记</span>

      <input
        v-model="draftLabel"
        class="label-input"
        type="text"
        maxlength="120"
        :placeholder="browsing ? '标签（可选），标记当前时刻' : '标签（可选），标记最新弹幕时刻'"
        :disabled="playheadTs == null || busy"
        @keyup.enter="submit"
      />
      <button
        class="mini primary"
        :disabled="playheadTs == null || busy"
        :title="playheadTs == null ? '还没有可标记的弹幕' : `标记在 ${stamp(playheadTs)}`"
        @click="submit"
      >
        ＋ 打标记
      </button>
      <span v-if="playheadTs != null" class="hint">将标记在 {{ stamp(playheadTs) }}</span>
      <span v-else class="hint">暂无可标记的时刻（等一条弹幕）</span>

      <span class="spacer"></span>

      <span class="count">{{ bookmarks.length }} 个</span>
      <button v-if="bookmarks.length" class="mini" title="展开/收起标记列表" @click="collapsed = !collapsed">
        {{ collapsed ? '展开' : '收起' }}
      </button>
      <button
        v-if="bookmarks.length"
        class="mini"
        :disabled="busy"
        title="删除本房间的全部标记（只动标注库，不碰采集数据）"
        @click="emit('clear')"
      >
        清空
      </button>
    </div>

    <div v-if="bookmarks.length && !collapsed" class="list">
      <div
        v-for="item in bookmarks"
        :key="item.id"
        class="mark"
        :class="{ active: item.id === activeId }"
        :title="`${stamp(item.recv_ts)}${item.note ? ' · ' + item.note : ''}`"
      >
        <button class="jump-btn" @click="emit('jump', item.recv_ts)">
          <span class="dot"></span>
          <span class="stamp">{{ stamp(item.recv_ts) }}</span>
          <span class="label">{{ item.label }}</span>
          <span v-if="item.anchor_id" class="anchor-tag" title="钉在某条弹幕上">钉</span>
        </button>
        <button
          class="mini pick"
          :class="{ picked: item.id === activeId }"
          title="选中它作为导出区间的起点"
          @click="emit('select', item.id)"
        >
          {{ item.id === activeId ? '已选' : '选为起点' }}
        </button>
        <button class="mini remove" title="删除这个标记" @click="emit('remove', item.id)">×</button>
      </div>

      <div class="export-row">
        <template v-if="active">
          <span class="hint">
            从「{{ active.label }}」（{{ stamp(active.recv_ts) }}）导出
            <template v-if="activeIndex >= 0 && activeIndex < bookmarks.length - 1">
              到下一个标记「{{ bookmarks[activeIndex + 1].label }}」
            </template>
            <template v-else>到数据末尾</template>
          </span>
          <button class="mini accent" @click="exportRange('csv')">导出该区间 CSV</button>
          <button class="mini" @click="exportRange('jsonl')">JSONL</button>
        </template>
        <span v-else class="hint">点某个标记的「选为起点」即可按标记区间导出</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.bookmark-bar {
  border-top: 1px solid var(--border);
  background: var(--panel);
  padding: 5px 14px 7px;
  font-size: 12px;
}
.row { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
.title { color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; font-weight: 600; font-size: 11px; }
.hint { color: var(--muted); font-size: 11px; }
.spacer { flex: 1; }
.count { color: var(--muted); font-variant-numeric: tabular-nums; }
.label-input {
  width: 190px; padding: 3px 8px; font-size: 12px;
  background: #0f1115; color: inherit; border: 1px solid var(--border); border-radius: 6px;
}
.mini { padding: 2px 9px; font-size: 12px; }
.mini.primary { color: var(--accent); border-color: #1e4d63; }
.mini.accent { color: var(--accent); border-color: #1e4d63; }
.mini.remove { padding: 1px 7px; }
.mini.pick.picked { color: var(--bg); background: var(--accent); border-color: var(--accent); font-weight: 600; }

.list { margin-top: 6px; display: flex; flex-direction: column; gap: 3px; max-height: 150px; overflow-y: auto; }
.mark { display: flex; align-items: center; gap: 5px; border-radius: 5px; padding: 1px 2px; }
.mark:hover { background: #1a1d23; }
.mark.active { background: #16232c; }
.jump-btn {
  flex: 1; min-width: 0; display: flex; align-items: center; gap: 7px;
  background: none; border: none; padding: 2px 4px; text-align: left; cursor: pointer;
}
.jump-btn:hover { color: var(--accent); }
.dot { width: 6px; height: 6px; border-radius: 50%; background: #fbbf24; flex-shrink: 0; }
.stamp { color: #9ecbf5; font-variant-numeric: tabular-nums; flex-shrink: 0; }
.label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.anchor-tag {
  font-size: 10px; color: #a8c0d8; border: 1px solid #3d4b5c; border-radius: 3px; padding: 0 3px; flex-shrink: 0;
}
.export-row { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; margin-top: 4px; }
</style>
