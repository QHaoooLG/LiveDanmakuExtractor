"""验证「一个房间都没添加」时的引导横幅（优化项 ⑦）。

需求：web 端在房间列表为空时提示用户去选择/添加房间。

只读组件源码不够 —— `v-if="roomCount === 0"` 这种条件写在真渲染路径上才算数，
所以这里用真 Vue 的 `renderToString` 把 `EmptyRoomsBanner.vue` 渲两遍：
   * `roomCount = 0` → 横幅必须在，且**说清了下一步做什么**（不能只是"暂无数据"）；
   * `roomCount = 1` → 横幅必须消失（否则就是永久噪音）。

顺带钉住两个容易退化的文案点：
   * 必须明确"程序在正常运行" —— 否则用户会误判成没启动成功，去点停止/重开；
   * 必须**不提登录** —— web 面板上没有扫码入口（只有托盘启动面板有），
     在一个做不到的界面上提"去登录"会把用户带进死胡同。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"

SCRIPT = """
// 用 Vite 的 `ssrLoadModule` 编译 .vue —— node 原生不认识 .vue 扩展名。
import { createServer } from 'vite'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'

const server = await createServer({
  root: process.cwd(),
  logLevel: 'error',
  server: { middlewareMode: true },
  appType: 'custom',
})

try {
  const mod = await server.ssrLoadModule('/src/components/EmptyRoomsBanner.vue')
  const Banner = mod.default

  async function render(roomCount) {
    const app = createSSRApp({ render: () => h(Banner, { roomCount }) })
    return await renderToString(app)
  }

  const empty = await render(0)
  const one = await render(1)
  const many = await render(3)
  process.stdout.write('RESULT ' + JSON.stringify({ empty, one, many }))
} finally {
  await server.close()
}
"""

print("=" * 80)
print("用真 Vue SSR 渲染 EmptyRoomsBanner.vue，检查空状态引导")
print("=" * 80)

result = subprocess.run(
    ["node", "--input-type=module", "-e", SCRIPT],
    cwd=WEB, capture_output=True, text=True, encoding="utf-8",
    errors="replace", timeout=180,
)
if result.returncode != 0:
    print("  ❌ 渲染失败：")
    print((result.stderr or "")[-1500:])
    raise SystemExit(1)

payload = None
for line in (result.stdout or "").splitlines():
    if line.startswith("RESULT "):
        payload = json.loads(line[len("RESULT "):])
if payload is None:
    print("  ❌ 拿不到渲染结果")
    print((result.stdout or "")[:500])
    raise SystemExit(1)

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {label}{('  ' + detail) if (detail and not ok) else ''}")
    if not ok:
        failures.append(label)


empty, one, many = payload["empty"], payload["one"], payload["many"]

print()
print("1. 没有房间 → 必须出现引导")
print("=" * 80)
check("横幅出现", "empty-banner" in empty)
check("给出标题", "还没有要采集的房间" in empty)
check("先说明程序是正常的（否则用户会以为没启动成功）", "正常运行" in empty)
check("给出具体可照抄的直播间链接", "live.bilibili.com/8432038" in empty)
check("有「去添加房间」按钮", "去添加房间" in empty)
check("不提登录（web 面板上没有扫码入口）", "登录" not in empty, "── 会把用户带进死胡同")

print()
print("2. 已有房间 → 必须消失（否则是永久噪音）")
print("=" * 80)
check("1 个房间时不显示", "empty-banner" not in one)
check("3 个房间时不显示", "empty-banner" not in many)

print()
print("=" * 80)
if failures:
    print(f"❌ 失败 {len(failures)} 项：{failures}")
    raise SystemExit(1)
print("✅ 空房间时确实会引导用户去添加房间")
print("=" * 80)
