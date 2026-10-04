<script setup>
/**
 * 空状态引导横幅：**一个房间都没添加**时该怎么继续。
 *
 * 需求背景：原来右侧列表里只有一行灰字「尚未监控任何房间 / 在上方输入房间号后点
 * 「添加」」。问题是"上方"只是列表内部的上方 —— 面板首屏那么多控件，
 * 用户在中间的大片空白里得不到任何指示，往往以为程序没工作。
 *
 * 两条设计原则：
 *
 * 1. **只说下一步做什么，不说"暂无数据"**。空面板本身已经说明了"没数据"，
 *    重复一遍只会占地方。所以这里给的是可执行的动作。
 * 2. **承接的是"浏览器里打开的页面"这一事实**。用这份面板的人手里已经有
 *    一个正在运行的采集进程，所以第一句要先说"程序在正常运行" ——
 *    否则用户的第一反应是"是不是没启动成功"，然后去点停止/重开。
 *
 * 关闭语义：与 `HealthBanner` 不同，这里**不给关闭按钮**。它不是提示噪音，
 * 而是"当前状态下唯一有意义的下一步"；关掉之后面板又回到完全没有指引的样子。
 * 房间一加上（`rooms` 非空）它就自己消失。
 *
 * 刻意**不提登录**：web 面板上没有登录入口（扫码要图形界面，只有托盘里的启动
 * 面板能做），提了只会让用户在一个做不到的界面上找按钮。没凭证时的问题由
 * `HealthBanner` 在**采集真的失败之后**报出来，那时再说才准确。
 */
defineProps({
  /** 当前监控中的房间数；为 0 才显示 */
  roomCount: { type: Number, default: 0 },
})

const emit = defineEmits(['add-room'])
</script>

<template>
  <div v-if="roomCount === 0" class="empty-banner">
    <span class="empty-title">还没有要采集的房间</span>
    <span class="empty-detail">
      程序已经在正常运行，只差一个房间号。在右侧「监控中」上方粘贴
      <code>https://live.bilibili.com/8432038</code> 这样的链接，或直接填房间号。
    </span>
    <button
      class="empty-action"
      title="把光标移到右侧的房间号输入框（可以直接粘贴直播间链接）"
      @click="emit('add-room')"
    >
      去添加房间
    </button>
  </div>
</template>

<style scoped>
.empty-banner {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin: 0 0 8px;
  padding: 9px 14px;
  border-radius: 8px;
  border: 1px solid #1e4d63;
  background: linear-gradient(90deg, #12242e 0%, #15181d 100%);
  color: #6cc7f5;
}
.empty-title { font-weight: 700; flex-shrink: 0; }
.empty-detail { color: #c9c9c9; font-size: 12px; line-height: 1.5; word-break: break-word; }
.empty-detail code {
  color: #9ad9ff;
  background: rgba(108, 199, 245, 0.1);
  border-radius: 4px;
  padding: 0 4px;
}
.empty-action {
  flex-shrink: 0;
  background: none;
  border: 1px solid currentColor;
  color: inherit;
  border-radius: 6px;
  padding: 1px 10px;
  font-size: 12px;
  cursor: pointer;
}
.empty-action:hover { background: rgba(255, 255, 255, 0.08); }
</style>
