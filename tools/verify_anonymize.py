"""端到端验证「匿名昵称」真的生效（需求 3）。

只测纯函数不够 —— `maskName()` 单测过了也说明不了**它被用在了渲染路径上**
（上一轮就是"函数写对了、但开关做成了别的功能"）。

这里用真 Vue 的 `renderToString` 把 `StreamList.vue` 渲出来，检查：
  * 默认（anonymizeName=false）→ 显示**完整昵称**；
  * 开启（anonymizeName=true）→ 显示 `首字符 + *`，且**真名不出现**在输出里。
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"

SCRIPT = textwrap.dedent(
    """
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
      const mod = await server.ssrLoadModule('/src/components/StreamList.vue')
      const StreamList = mod.default

      const events = [
        { id: 1, type: 'danmaku', recv_ts: 1790910000, uid: 1000, uname: '六式zy', text: '测试弹幕' },
        { id: 2, type: 'danmaku', recv_ts: 1790910001, uid: 2000, uname: '张三丰', text: '第二条' },
      ]

      async function render(anonymizeName) {
        const app = createSSRApp({
          render: () => h(StreamList, { events, anonymizeName }),
        })
        return await renderToString(app)
      }

      const normal = await render(false)
      const masked = await render(true)
      process.stdout.write('RESULT ' + JSON.stringify({ normal, masked }))
    } finally {
      await server.close()
    }
    """
)

script_path = ROOT / "build" / "_probe_anon.mjs"
script_path.parent.mkdir(parents=True, exist_ok=True)
script_path.write_text(SCRIPT, encoding="utf-8")

print("=" * 80)
print("用真 Vue SSR 渲染 StreamList.vue，检查昵称渲染")
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

normal, masked = payload["normal"], payload["masked"]
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    # 只在失败时附上 detail（detail 写的是"失败意味着什么"，成功时不该显示）
    print(f"  {'✅' if ok else '❌'} {label}{('  ' + detail) if (detail and not ok) else ''}")
    if not ok:
        failures.append(label)


print()
print("1. 默认（不匿名）")
print("=" * 80)
check("显示完整昵称「六式zy」", "六式zy" in normal)
check("显示完整昵称「张三丰」", "张三丰" in normal)

# 只检查**昵称那一栏**里的内容，不整页找 '*' ——
# `title` 属性与关键字高亮的 `<mark>` 都可能带星号，整页找会误判。
import re


def names_of(html: str) -> list[str]:
    """抽出昵称 `span` 里的**显示文本**。

    类名顺序不可靠：Vue 对静态 + 动态类会渲染成 `"anonymized name"`（不是我写的顺序），
    所以用"类名里含 `name` 这个词"来匹配，而不是匹配 `class="name"` 字面量。
    同时必须排除 `title="..."`（那里的属性值会干扰后面的 `[^>]*>`）。
    """
    spans = re.findall(r'<span class="([^"]*)"[^>]*>([^<]*)</span>', html)
    return [text for classes, text in spans if re.search(r"\bname\b", classes)]


normal_names = names_of(normal)
check("昵称栏没有星号", all("*" not in name for name in normal_names), f"{normal_names}")

print()
print("2. 打开匿名")
print("=" * 80)
masked_names = names_of(masked)
check("首字符保留（六）", any(name.startswith("六") for name in masked_names), f"{masked_names}")
check("星号已出现", all("*" in name for name in masked_names), f"{masked_names}")
check("**真名不再出现**（六式zy）", "六式zy" not in masked, "── 泄露了真名！")
check("**真名不再出现**（张三丰）", "张三丰" not in masked, "── 泄露了真名！")
check("匿名的昵称都是 首字符+星号",
      bool(masked_names) and all(re.fullmatch(r".\*+", name, re.S) for name in masked_names),
      f"{masked_names}")

print()
print(f"  默认渲染出的昵称 = {normal_names}")
print(f"  匿名渲染出的昵称 = {masked_names}")

script_path.unlink(missing_ok=True)

print()
print("=" * 80)
if failures:
    print(f"❌ 失败 {len(failures)} 项：{failures}")
    raise SystemExit(1)
print("✅ 匿名昵称开关在真实渲染路径上生效")
print("=" * 80)
