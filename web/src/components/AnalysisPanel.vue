<script setup>
/**
 * 感知分析面板（V3-b 3A）：词频榜 / 热点突增 / 疑似刷屏 / 疑似异常账号。
 *
 * 三条使用约定（改之前先读）：
 *
 * 1. **数据不出本机**：全部由后端在内存窗口上算（`/api/analysis`），
 *    前端不做任何外发。面板底部那行说明是**承诺**，别删。
 * 2. **措辞必须是"疑似/仅供参考"**：`suspicious` 是启发式打分，不是判定。
 *    把分数包装成结论，用户就可能据此误伤真人。
 *    理由（`reasons`）必须显示出来，让用户能自己复核。
 * 3. **点词 → 过滤弹幕流**：复用顶栏的关键词过滤（`update:keyword`），
 *    不另造一套筛选状态 —— 否则用户会看到"两个过滤条件互相打架"。
 */
import { computed, ref } from 'vue'

const props = defineProps({
  /** `/api/analysis` 的响应 */
  analysis: { type: Object, default: null },
  /** 当前是否在"全部房间"（多房间合并时的角标说明） */
  allRooms: { type: Boolean, default: false },
})

const emit = defineEmits(['filter-word'])

/** 各区块是否展开（默认只看词频，其余收起 —— 右栏寸土寸金） */
const open = ref({ words: true, hotspot: true, spam: false, suspicious: false })

function toggle(key) {
  open.value = { ...open.value, [key]: !open.value[key] }
}

const words = computed(() => props.analysis?.words?.items ?? [])
const hotspot = computed(() => props.analysis?.hotspot?.items ?? [])
const hotspotNote = computed(() => props.analysis?.hotspot?.note ?? '')
const spam = computed(() => props.analysis?.spam?.items ?? [])
const suspicious = computed(() => props.analysis?.suspicious?.items ?? [])

/** 词频条按榜首归一化（只用于视觉长度，不改变数字口径）。 */
const peak = computed(() => words.value.reduce((max, item) => Math.max(max, item.count), 1))

const windowLabel = computed(() => {
  const payload = props.analysis
  if (!payload) return '加载中…'
  const seconds = Math.round(payload.window_s ?? 0)
  const span = seconds >= 60 ? `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒` : `${seconds} 秒`
  return `${payload.messages} 条 / 跨 ${span}`
})

function barWidth(item) {
  return `${Math.max((item.count / peak.value) * 100, 4)}%`
}
</script>

<template>
  <section class="analysis">
    <h3>
      感知分析
      <span class="scope">{{ windowLabel }}</span>
      <span v-if="allRooms" class="scope" title="多房间合并统计">全部房间</span>
    </h3>

    <div v-if="!analysis" class="empty" style="padding:6px 0;text-align:left;font-size:12px">
      分析加载中…
    </div>

    <template v-else>
      <!-- 词频榜：点词即可把弹幕流切到"只看含该词" -->
      <div class="block-head" @click="toggle('words')">
        <span class="caret">{{ open.words ? '▾' : '▸' }}</span>
        <span>实时词频</span>
        <span class="count">{{ words.length }}</span>
        <span class="hint">点词可只看含该词的弹幕</span>
      </div>
      <div v-if="open.words" class="words">
        <button
          v-for="item in words"
          :key="item.word"
          class="word"
          type="button"
          :title="`${item.count} 次${item.users ? ` · ${item.users} 人说过` : ''}（点击只看含「${item.word}」的弹幕）`"
          @click="emit('filter-word', item.word)"
        >
          <span class="word-text">{{ item.word }}</span>
          <span class="word-bar"><span class="fill" :style="{ width: barWidth(item) }"></span></span>
          <span class="word-count">{{ item.count }}</span>
          <span v-if="item.users" class="word-users" :title="`${item.users} 个不同的人说过`">
            {{ item.users }}人
          </span>
        </button>
        <div v-if="!words.length" class="hint-line">窗口内还没有可统计的弹幕</div>
      </div>

      <!-- 热点突增：当前片 vs 之前若干片的篇均速率 -->
      <div class="block-head" @click="toggle('hotspot')">
        <span class="caret">{{ open.hotspot ? '▾' : '▸' }}</span>
        <span>热点突增</span>
        <span class="count">{{ hotspot.length }}</span>
      </div>
      <div v-if="open.hotspot" class="hotspot">
        <div v-for="item in hotspot" :key="item.word" class="hot-item">
          <span class="hot-word">{{ item.word }}</span>
          <span class="hot-count">{{ item.count }} 次</span>
          <span v-if="item.is_new" class="tag-new" title="之前的时间片里没出现过">新词</span>
          <span v-else class="hot-ratio" :title="`基线篇均 ${item.baseline} 次`">
            ×{{ item.ratio }}
          </span>
        </div>
        <div v-if="!hotspot.length" class="hint-line">
          {{ hotspotNote || '暂无明显突增（样本不足时会显示样本不足）' }}
        </div>
      </div>

      <!-- 疑似刷屏（同一文本被反复发送） -->
      <div class="block-head" @click="toggle('spam')">
        <span class="caret">{{ open.spam ? '▾' : '▸' }}</span>
        <span>疑似刷屏</span>
        <span class="count">{{ spam.length }}</span>
      </div>
      <div v-if="open.spam" class="spam">
        <div v-for="item in spam" :key="item.text" class="spam-item" :title="item.text">
          <span class="spam-text">{{ item.text }}</span>
          <span class="spam-meta">
            {{ item.count }} 次 · {{ item.users }} 人
            <template v-if="item.single_user"> · <em class="warn">同一人</em></template>
            <template v-else> · 跟风</template>
          </span>
        </div>
        <div v-if="!spam.length" class="hint-line">暂无（短句跟风不算，避免淹没真正的异常）</div>
      </div>

      <!-- 疑似异常账号：启发式打分 + 必须给出理由 -->
      <div class="block-head" @click="toggle('suspicious')">
        <span class="caret">{{ open.suspicious ? '▾' : '▸' }}</span>
        <span>疑似异常账号</span>
        <span class="count">{{ suspicious.length }}</span>
      </div>
      <div v-if="open.suspicious" class="suspicious">
        <div v-for="item in suspicious" :key="item.uid" class="susp-item">
          <div class="susp-line">
            <span class="susp-name" :title="`uid ${item.uid}`">{{ item.uname ?? item.uid }}</span>
            <span class="susp-score">{{ item.score }}</span>
          </div>
          <div class="susp-reasons">
            <span v-for="(reason, index) in item.reasons" :key="index">{{ reason }}</span>
          </div>
        </div>
        <div v-if="!suspicious.length" class="hint-line">暂无</div>
      </div>

      <div class="footnote">
        本地启发式统计，仅供参考、非判定；数据不出本机。
        <template v-if="analysis.window_cap">
          （窗口上限 {{ analysis.window_cap }} 条）
        </template>
      </div>
    </template>
  </section>
</template>

<style scoped>
.analysis { margin-top: 14px; border-top: 1px solid var(--border); padding-top: 2px; }
h3 .scope {
  margin-left: 6px; font-size: 11px; font-weight: 400;
  color: #8b93a1; border: 1px solid #2c313a; border-radius: 999px; padding: 1px 7px;
}

.block-head {
  display: flex; align-items: center; gap: 5px;
  font-size: 12px; color: var(--muted); cursor: pointer; padding: 3px 0;
  user-select: none;
}
.block-head:hover { color: var(--text); }
.block-head .caret { width: 10px; }
.block-head .count {
  border: 1px solid var(--border); border-radius: 999px; padding: 0 6px; font-size: 11px;
}
.block-head .hint { margin-left: auto; font-size: 10px; }
.hint-line { color: var(--muted); font-size: 11px; padding: 2px 0 4px; }

/* 词频：一行一个词，条长按榜首归一化，右侧是次数与人数 */
.words { display: flex; flex-direction: column; gap: 2px; }
.word {
  display: grid; grid-template-columns: 1fr 70px auto auto;
  align-items: center; gap: 6px;
  background: none; border: none; border-radius: 4px;
  padding: 2px 4px; cursor: pointer; text-align: left; font-size: 12px;
}
.word:hover { background: #1a1d23; color: var(--accent); }
.word-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.word-bar { background: #0f1115; border-radius: 3px; height: 9px; overflow: hidden; }
.word-bar .fill { display: block; height: 100%; background: #3f6d8e; border-radius: 3px; }
.word:hover .word-bar .fill { background: var(--accent); }
.word-count { font-variant-numeric: tabular-nums; color: var(--text); min-width: 28px; text-align: right; }
.word-users { font-size: 10px; color: var(--muted); min-width: 30px; text-align: right; }

.hotspot, .spam, .suspicious { display: flex; flex-direction: column; gap: 3px; padding-bottom: 4px; }
.hot-item { display: flex; align-items: center; gap: 6px; font-size: 12px; }
.hot-word { color: #fbbf24; }
.hot-count { color: var(--muted); font-size: 11px; }
.hot-ratio { color: #f472b6; font-size: 11px; font-variant-numeric: tabular-nums; }
.tag-new {
  font-size: 10px; color: #86efac; border: 1px solid #14532d; border-radius: 3px; padding: 0 4px;
}

.spam-item { font-size: 12px; line-height: 1.4; }
.spam-text { color: #fca5a5; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; display: block; }
.spam-meta { color: var(--muted); font-size: 11px; }
.spam-meta em { font-style: normal; color: #fbbf24; }

.susp-item { font-size: 12px; line-height: 1.4; margin-bottom: 2px; }
.susp-line { display: flex; justify-content: space-between; gap: 6px; }
.susp-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.susp-score {
  font-variant-numeric: tabular-nums; color: #fbbf24; flex-shrink: 0;
  font-weight: 600;
}
.susp-reasons { display: flex; flex-direction: column; color: var(--muted); font-size: 10px; }

.footnote { color: #5b6472; font-size: 10px; line-height: 1.5; padding-top: 4px; }
</style>
