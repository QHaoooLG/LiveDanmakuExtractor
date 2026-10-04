"""验证时间轴 canvas 版（优化项 ⑩）真的在画东西。

## 为什么需要它（`web/test/density.test.mjs` 已经在测了）

那个是**单元级**的断言（"某格用了焦点色"这种），跑在 `node --test` 里。
这个脚本是**端到端冒烟**：从 Python 侧起一个 Node 进程，用真组件 + 真 jsdom
把一整屏画一遍，然后检查"这一屏到底有没有内容"：

  * 柱子数量 == 有数据的桶数；
  * 柱高随条数**单调递增**（归一化算错时最容易出的是"每根一样高"）；
  * 最高的那根接近满高、最矮的非零根不低于 4%（需求：稀疏区不能整段消失）；
  * 词云那类"画布空白"的失败模式（ops 里只有 clearRect）能立刻被抓到。

用 Python 驱动的理由与其它 `tools/verify_*.py` 一致：结果落成能贴进交接文档的
表格，而不是 Node 测试框架里的一行 `ok`。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"

SCRIPT = """
import { makeDensity, mountComponent, setBox, setupDom, stubCanvasPrototype } from './test/helpers/vue.mjs'

setupDom()
const DensityCanvas = (await import('./src/components/DensityCanvas.vue')).default

// 故意造出不均匀的数据：归一化写错时（例如把 max 当峰值、或忘了判 0）
// "每根一样高"与"全零桶也画柱子"都会在这里露馅
const counts = [0, 1, 2, 40, 0, 7, 120, 3, 0, 55]
const density = makeDensity({ count: counts.length, counts })

const ctx = stubCanvasPrototype()
const wrapper = await mountComponent(DensityCanvas, { density }, { emits: ['jump'] })
const scroller = wrapper.find('.track-scroll').element
setBox(scroller, { clientWidth: 600, clientHeight: 46, scrollWidth: 600 })
await wrapper.nextTick()

// 柱子 = y > 0 的 fillRect；背景格是 y == 0
const bars = ctx.ops.filter((op) => op.op === 'fillRect' && op.y > 0)
const background = ctx.ops.filter((op) => op.op === 'fillRect' && op.y === 0)
process.stdout.write('RESULT ' + JSON.stringify({
  counts,
  canvasWidth: wrapper.find('canvas').element.width,
  canvasHeight: wrapper.find('canvas').element.height,
  clearRect: ctx.calls.clearRect,
  setTransform: ctx.calls.setTransform,
  background: background.length,
  bars: bars.map((bar) => ({ x: bar.x, y: bar.y, h: bar.h, w: bar.w })),
  headText: wrapper.find('.density-head').text().replace(/改用 (canvas|DOM)/, '').trim(),
}))
"""

print("=" * 84)
print("用真 Vue + jsdom 挂 DensityCanvas，检查一整屏都画了什么")
print("=" * 84)

result = subprocess.run(
    ["node", "--import", "./test/register-vue.mjs", "--input-type=module", "-e", SCRIPT],
    cwd=WEB, capture_output=True, text=True, encoding="utf-8",
    errors="replace", timeout=300,
)
if result.returncode != 0:
    print("  ❌ 渲染失败：")
    print((result.stderr or "")[-2000:])
    raise SystemExit(1)

payload = None
for line in (result.stdout or "").splitlines():
    if line.startswith("RESULT "):
        payload = json.loads(line[len("RESULT "):])
if payload is None:
    print("  ❌ 拿不到渲染结果：")
    print((result.stdout or "")[:600])
    raise SystemExit(1)

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {label}{('  ' + detail) if (detail and not ok) else ''}")
    if not ok:
        failures.append(label)


counts = payload["counts"]
bars = payload["bars"]
non_zero = [value for value in counts if value > 0]

print()
print("1. 基本绘制")
print("=" * 84)
check("清过屏", payload["clearRect"] >= 1, f"clearRect={payload['clearRect']}")
check("按 DPR 设了变换", payload["setTransform"] >= 1, f"setTransform={payload['setTransform']}")
check(
    "画布后备缓冲 = 视口宽 × 高（DPR=1）",
    payload["canvasWidth"] == 600 and payload["canvasHeight"] == 46,
    f"{payload['canvasWidth']}×{payload['canvasHeight']}",
)

print()
print("2. 柱子数量与背景格数量")
print("=" * 84)
check(
    f"每格都画了背景（{len(counts)} 格，空格子也要能点）",
    payload["background"] == len(counts),
    f"background={payload['background']}",
)
check(
    f"柱子数 = 有数据的桶数（{len(non_zero)}）",
    len(bars) == len(non_zero),
    f"bars={len(bars)}",
)

print()
print("3. 柱高与条数的关系（归一化对不对）")
print("=" * 84)
if bars:
    heights = [bar["h"] for bar in bars]
    ordered = [bar for _, bar in sorted(zip(non_zero, bars), key=lambda pair: pair[0])]
    ascending = [bar["h"] for bar in ordered]
    check(
        "柱高随条数单调递增（不是每根一样高）",
        all(b >= a - 1e-9 for a, b in zip(ascending, ascending[1:])),
        f"排序后高度={[round(value, 1) for value in ascending]}",
    )
    check(
        "最高的柱子接近满高",
        max(heights) >= 46 * 0.95,
        f"最高={max(heights):.1f}（视口高 46）",
    )
    check(
        "最矮的非零柱子不低于 4%（稀疏区不能整段消失）",
        min(heights) >= 46 * 0.04 - 1e-9,
        f"最矮={min(heights):.1f}",
    )
    check("柱子宽度为正", all(bar["w"] > 0 for bar in bars))
    print(f"     heights(按条数升序) = {[round(value, 1) for value in ascending]}")
    print(f"     counts (按条数升序) = {sorted(non_zero)}")

print()
print("4. 表头（共用组件，和 DOM 版一字不差）")
print("=" * 84)
head = payload["headText"]
check("含标题", "时间轴" in head)
check("含每格时长", "每格" in head)
check("含总量", f"共 {sum(counts)} 条" in head, f"实际 {head!r}")

print()
print("=" * 84)
if failures:
    print(f"❌ 失败 {len(failures)} 项：{failures}")
    raise SystemExit(1)
print("✅ canvas 版时间轴确实画出了内容（柱数/柱高/表头都对）")
print("=" * 84)

if "--json-out" in sys.argv:
    out = Path(sys.argv[sys.argv.index("--json-out") + 1])
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"（原始绘制记录已写入 {out}）")
