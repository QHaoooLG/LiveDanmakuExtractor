<script setup>
/**
 * 类型分布：**横向柱状图**。
 *
 * 设计要点（改之前先读）：
 *   * 一行一个类型：左侧类型名 → 中间彩色横条 → **右侧条数**（需求明确要求条数在右）；
 *   * 每个类型一种颜色（`typeColor()`，与弹幕行高亮同一套色），
 *     而不是所有条目共用一个 accent 色 —— 这样一眼能对上流里的颜色；
 *   * 条长按**占比**（count / 总数）而不是相对最大值：相对最大值在只有
 *     两三种类型时会给出"满格"的误导感，占比才能反映真实分布；
 *   * 占比同时以数字写在条右侧，所以"条长"只是辅助，读数不依赖肉眼估长。
 */
import { computed } from 'vue'
import { typeColor, typeLabel } from '../format.js'

const props = defineProps({
  /** `{ type: count }`，通常来自 `/api/stats.by_type` */
  byType: { type: Object, default: () => ({}) },
  /** 空数据的提示语 */
  emptyText: { type: String, default: '该范围内无数据' },
})

/** 按条数降序；条长按占总数的百分比。 */
const rows = computed(() => {
  const entries = Object.entries(props.byType ?? {}).filter(([, count]) => Number(count) > 0)
  const total = entries.reduce((sum, [, count]) => sum + Number(count), 0)
  return entries
    .sort((a, b) => Number(b[1]) - Number(a[1]))
    .map(([type, count]) => {
      const value = Number(count)
      const share = total ? (value / total) * 100 : 0
      return {
        type,
        label: typeLabel(type),
        color: typeColor(type),
        count: value,
        // 极小占比也留 1% 宽度，否则"有这类但几乎看不见"会被当成 0
        width: `${Math.max(share, share > 0 ? 1.5 : 0)}%`,
        share: share >= 10 ? `${Math.round(share)}%` : `${share.toFixed(1)}%`,
      }
    })
})

const total = computed(() => rows.value.reduce((sum, row) => sum + row.count, 0))
</script>

<template>
  <div v-if="rows.length" class="type-bars">
    <div v-for="row in rows" :key="row.type" class="tb-row" :title="`${row.type} · ${row.count} 条 · ${row.share}`">
      <span class="tb-label" :style="{ color: row.color }">{{ row.label }}</span>
      <span class="tb-track">
        <span class="tb-fill" :style="{ width: row.width, background: row.color }"></span>
      </span>
      <span class="tb-count">{{ row.count }}</span>
    </div>
    <div class="tb-total">合计 {{ total }} 条</div>
  </div>
  <div v-else class="empty" style="padding:10px 0;text-align:left">{{ emptyText }}</div>
</template>

<style scoped>
.type-bars { display: flex; flex-direction: column; gap: 6px; }
/* 标签固定宽 → 所有横条起点对齐，读数时才不会左右乱跳 */
.tb-row { display: grid; grid-template-columns: 52px 1fr 46px; gap: 8px; align-items: center; font-size: 12px; }
.tb-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tb-track { background: #0f1115; border: 1px solid #23272f; border-radius: 4px; height: 12px; overflow: hidden; }
.tb-fill { display: block; height: 100%; border-radius: 3px; min-width: 2px; transition: width 0.25s ease; }
.tb-count { text-align: right; font-variant-numeric: tabular-nums; color: var(--text); }
.tb-total { font-size: 11px; color: var(--muted); text-align: right; }
@media (prefers-reduced-motion: reduce) {
  .tb-fill { transition: none; }
}
</style>
