<script setup>
/**
 * 右侧信息栏：**统计信息**（房间列表已移到 `RoomPanel.vue`）。
 *
 * 与更早版本的区别：
 *   * 「服务」栏目已删除 —— 版本/落盘格式/脱敏这些是**部署期**信息，
 *     面板开着的时候没人反复看；需要时用 `bili-danmaku web status` / `/api/health` 查；
 *   * **「速率 / 延迟」整块已删除**（需求 2026-10-09 #1：不再显示到 web 端）——
 *     连同「条/分钟」「延迟 P50 / P95」「数据源」「订阅者 / 丢弃」一起拿掉。
 *     这些是**采集器自检**指标（消息条数看的是 hub 内存计数、延迟是接收时刻与
 *     服务端时间戳之差、订阅者/丢弃是广播背压），不是"这场直播发生了什么"，
 *     放在信息栏里既占地方又容易和弹幕统计混淆。需要排查时看
 *     `/api/stats`（字段仍在）或 `bili-danmaku web logs`；
 *   * 「类型分布」是横向彩色柱状图（`TypeBars.vue`）；
 *   * 「发言 TOP」在**累计口径下也显示**（数据来自 SQLite 而不是 hub 内存计数）。
 */
import { computed } from 'vue'
import TypeBars from './TypeBars.vue'
import { topSendersHint } from './statsHint.js'

const props = defineProps({
  stats: { type: Object, default: null },
  /** 当前选中的房间；为 null 表示"全部房间" */
  current: { type: [Number, null], default: null },
})

/** 累计口径下要标清范围，否则"自启动累计"容易被误读成"全部历史"。 */
const modeLabel = computed(() => {
  if (!props.stats) return ''
  return props.stats.mode === 'cumulative' ? '从启动累计' : `最近 ${props.stats.window_s}s`
})

const byType = computed(() => props.stats?.by_type ?? {})
const topSenders = computed(() => props.stats?.top_senders ?? [])

/**
 * 发言榜的**范围角标**：两种口径的数据来源不同，必须分开标注，
 * 否则会被误读成同一个统计范围（口径混读是本项目反复强调的坑）。
 *
 * 累计口径下后端会回「本次采集以来」—— 曾经这里兜底写的是"该房间全部时间"，
 * 那正是改口径之前的旧行为（现在榜只算本次采集）。
 */
const topScope = computed(() => {
  if (!props.stats) return ''
  if (props.stats.mode === 'cumulative') return props.stats.top_senders_scope ?? '本次采集以来'
  return `最近 ${props.stats.window_s}s`
})

/**
 * 榜为空时的说明。
 *
 * 逻辑抽到 `statsHint.js`（纯函数，可离线单测）—— 这里曾用
 * `source === 'memory'` 判断"sqlite sink 没开"，而累计口径下 `source` 恒为
 * `'memory'`，导致新库上显示"启用 sqlite sink 后才有发言榜"这种**误导提示**
 * （sqlite 默认本来就是开着的）。详见该模块的注释与用例。
 */
const topHint = computed(() => topSendersHint(props.stats))
</script>

<template>
  <aside class="side-stats">
    <template v-if="props.stats">
      <!-- 「速率 / 延迟」整块已删除（需求 2026-10-09 #1）：不再显示到 web 端。
           这里刻意**不留**只读的"消息条数"之类残迹 —— 需求是"这条栏目不再出现"，
           留一半会让人以为栏目还在、只是少了几行。需要这些数字时看 `/api/stats`。 -->
      <h3>类型分布<span v-if="modeLabel" class="scope">{{ modeLabel }}</span></h3>
      <TypeBars
        :by-type="byType"
        :empty-text="modeLabel ? `该范围内无数据（${modeLabel}）` : '该范围内无数据'"
      />

      <h3>发言 TOP<span v-if="topScope" class="scope">{{ topScope }}</span></h3>
      <template v-if="topSenders.length">
        <div v-for="(sender, index) in topSenders" :key="sender.uid" class="rank">
          <span class="rank-no">{{ index + 1 }}</span>
          <span class="n" :title="sender.uname ?? ''">{{ sender.uname ?? sender.uid }}</span>
          <span class="c">{{ sender.count }}</span>
        </div>
      </template>
      <div v-else class="empty" style="padding:6px 0;text-align:left;font-size:12px">{{ topHint }}</div>
    </template>
    <div v-else class="empty" style="padding:10px 0;text-align:left">统计加载中…</div>
  </aside>
</template>

<style scoped>
h3 .scope {
  margin-left: 6px; font-size: 11px; font-weight: 400;
  color: #8b93a1; border: 1px solid #2c313a; border-radius: 999px; padding: 1px 7px;
}
.rank-no {
  color: #5b6472; font-size: 11px; font-variant-numeric: tabular-nums;
  width: 14px; flex-shrink: 0; text-align: right;
}
</style>
