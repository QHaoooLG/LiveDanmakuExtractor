"""用真 Chromium（headless + CDP）验证 canvas **真的在屏幕上画出了像素**。

## 为什么需要这一步

jsdom 里没有光栅化，所以 `web/test/density.test.mjs` 与
`tools/verify_timeline_canvas.py` 都只能断言"我们**调用**了哪些绘制指令"。
一个真实的失败模式是"指令调了、但画布是白的"（尺寸算错、被 CSS 盖住、
上层元素遮挡……）。这里用 CDP 直连真浏览器，读回一帧真像素。

## 判定方式（刻意设计过，别改成"看有没有内容"）

**不用**"有没有深色像素"——`<canvas>` 的透明区域在截图里就是深色背景，
那种判据永远为真，等于没测。改成画三条已知颜色的线，然后看底部那一行有几种
不同颜色，再做一次反向对照：

* 画了三条线（白/红/绿）→ 底部行至少 3 种颜色；
* **清空之后**同一行必须是均匀的 1 种。

两个方向都能证伪，不会给出虚假的通过。

用法::

    .\\.venv\\Scripts\\python.exe tools/verify_canvas_pixels.py --port 8795
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

#: 页面里跑的探针：画三条已知颜色的线，读回像素，再清空读一次。
#:
#: 顺手做一次"**刚画完立刻读**"的自检（`sanityRed`）：如果连这个都读不到红色，
#: 说明是浏览器侧没光栅化，而不是我们的判定逻辑写错了 —— 错误信息必须能分辨这两者。
PROBE = r"""
(() => {
  const canvas = document.createElement('canvas')
  canvas.width = 120
  canvas.height = 40
  document.body.appendChild(canvas)
  const ctx = canvas.getContext('2d')
  if (!ctx) return JSON.stringify({ error: 'getContext("2d") 返回 null' })

  // 自检：画一块红，立刻读回
  ctx.fillStyle = '#ff0000'
  ctx.fillRect(0, 0, 10, 10)
  const sanity = ctx.getImageData(0, 0, 1, 1).data
  const sanityRed = [sanity[0], sanity[1], sanity[2]]

  const colors = ['#ffffff', '#ff0000', '#00ff00']
  ctx.clearRect(0, 0, canvas.width, canvas.height)
  const paintedRows = []
  colors.forEach((color, index) => {
    ctx.fillStyle = color
    const y = canvas.height - 3 * (index + 1)
    ctx.fillRect(0, y, canvas.width, 3)
    paintedRows.push([y, y + 2])
  })
  const read = () => {
    const row = []
    for (let y = 0; y < canvas.height; y += 1) {
      const data = ctx.getImageData(0, y, canvas.width, 1).data
      for (let x = 0; x < canvas.width; x += 1) {
        row.push([data[x * 4], data[x * 4 + 1], data[x * 4 + 2]])
      }
    }
    return row
  }
  const painted = read()
  ctx.clearRect(0, 0, canvas.width, canvas.height)
  const blank = read()
  const distinct = (row) => new Set(row.map((px) => px.join(','))).size
  return JSON.stringify({
    sanityRed,
    paintedRows,
    paintedDistinct: distinct(painted),
    blankDistinct: distinct(blank),
    paintedSample: painted.slice(0, 3),
    canvasSize: [canvas.width, canvas.height],
  })
})()
"""


def evaluate(ws_url: str, expression: str, timeout: float = 30.0) -> object:
    """在页面里跑一段表达式并取回它的值（已 JSON 解码）。

    用 Node 24 自带的全局 `WebSocket` 发这一条 CDP 消息 —— 这样不必为一次探测
    引入 `websocket-client` 依赖。
    """
    script = f"""
const ws = new WebSocket({ws_url!r})
const payload = {{
  id: 1,
  method: 'Runtime.evaluate',
  params: {{ expression: {json.dumps(expression)}, returnByValue: true, awaitPromise: true }},
}}
ws.addEventListener('open', () => ws.send(JSON.stringify(payload)))
ws.addEventListener('message', (event) => {{
  const message = JSON.parse(event.data)
  if (message.id !== 1) return
  // 把**整条 CDP 响应**打出来（而不是只打 value）：Python 侧要能分辨
  // "页面里抛了异常"和"拿到了值"，两者都在 `result` 里。
  //
  // 打完立刻 `exit(0)`：不然下面那个 `setTimeout` 还会再打一行
  // `ERR timeout`（实测踩到），stdout 里就混进了失败标记。
  //
  // ⚠️ 这一段是 **f-string**，所以 `\\n` 必须先写成 `\\\\n`：
  // f-string 会把 `\\n` 变成真换行，插进 JS 字符串里就成了未闭合的引号
  // （实测报的是 `SyntaxError: Unexpected end of input`）。
  process.stdout.write('OK ' + JSON.stringify(message) + '\\n')
  process.exit(0)
}})
ws.addEventListener('error', (event) => {{
  process.stdout.write('ERR ' + JSON.stringify(String(event.message ?? event.type)))
  process.exit(1)
}})
setTimeout(() => {{ process.stdout.write('ERR timeout'); process.exit(2) }}, {int(timeout * 1000)})
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout + 20,
    )
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if result.returncode != 0 and not out:
        raise RuntimeError(f"node 退出码 {result.returncode}；stderr：\n{err[-2000:]}")
    # CDP 的响应里可能夹着别的事件（`Runtime.consoleAPICalled` 之类），
    # 所以只挑我们发出去的那一条 `id`，而不是拿整份 stdout 去 `json.loads`
    # （那正是 "Extra data: line 1 column N" 的来源）。
    #
    # ⚠️ 这里的 `out.splitlines()` 依赖"node 侧打的是真换行"。曾经踩到的坑是
    # **Python 源码里写 `'\\n'`** —— 在 Python 字面量里 `\\n` 是"反斜杠 + n"两个
    # 字符，插进 JS 就成了字面量 `\n`，于是整条 JSON 后面跟着字符 `\n`，
    # 报的是 `Extra data: line 1 column N`。所以 node 侧必须打**真换行**，
    # 而 Python 源码里要写 `"\n"`（一个反斜杠）。
    payload = None
    for line in out.splitlines():
        if not line.startswith("OK "):
            continue
        try:
            message = json.loads(line[3:])
        except ValueError:
            continue
        if isinstance(message, dict) and "result" in message:
            payload = message["result"]
            break
    if payload is None:
        if out.startswith("ERR "):
            raise RuntimeError(out[4:])
        raise RuntimeError(
            f"CDP 没有返回可用结果；rc={result.returncode} "
            f"lines={[(line[:40], line.startswith('OK ')) for line in out.splitlines()]} "
            f"stderr={err[-600:]!r}"
        )

    exception = payload.get("exceptionDetails")
    if exception:
        detail = (exception.get("exception") or {}).get("description") or "evaluate failed"
        raise RuntimeError(f"页面里抛异常：{detail}")

    value = payload.get("result", {}).get("value")
    # 表达式返回的是字符串（`JSON.stringify(...)`），而 `returnByValue` 又把它当 JSON
    # 值回传 —— 所以可能还要再解一层。
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def find_browser() -> str:
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    raise SystemExit("找不到 Chrome/Edge —— 这一步需要真浏览器，跳过。")


def wait_for_devtools(port: int, timeout: float = 30.0) -> str:
    """等 `/json/list` 出现可用页面，返回它的 WebSocket 调试地址。"""
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3) as response:
                targets = json.loads(response.read().decode("utf-8"))
            for target in targets:
                if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                    return target["webSocketDebuggerUrl"]
            last_error = f"没有 type=page 的目标：{targets}"
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.5)
    raise SystemExit(f"等 DevTools 超时：{last_error}")


def main() -> int:
    parser = argparse.ArgumentParser(description="用真 Chromium 验证 canvas 光栅化")
    parser.add_argument("--port", type=int, default=8795, help="面板端口")
    parser.add_argument("--debug-port", type=int, default=9333, help="CDP 调试端口")
    args = parser.parse_args()

    browser = find_browser()
    print("=" * 84)
    print("用真 Chromium（headless + CDP）验证 canvas 光栅化")
    print("=" * 84)
    print(f"  浏览器：{browser}")

    profile = Path(tempfile.mkdtemp(prefix="canvas-check-"))
    process = subprocess.Popen(
        [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            f"--remote-debugging-port={args.debug_port}",
            f"--user-data-dir={profile}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  {'✅' if ok else '❌'} {label}{('  ' + detail) if (detail and not ok) else ''}")
        if not ok:
            failures.append(label)

    try:
        ws_url = wait_for_devtools(args.debug_port)
        print(f"  CDP 已就绪：{ws_url[:64]}…")

        # 先确认面板本身在真浏览器里能打开（不是降级页）
        with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/", timeout=10) as response:
            html = response.read().decode("utf-8", errors="replace")
        check("面板返回的是真前端（含 assets bundle）", "assets/index-" in html)

        payload = evaluate(ws_url, PROBE)
        if payload.get("error"):
            raise SystemExit(f"页面里拿不到 canvas：{payload['error']}")

        print()
        print("1. canvas 光栅化本身可用")
        print("=" * 84)
        check(
            "刚画完立刻读回是纯红（浏览器侧真的在光栅化）",
            payload["sanityRed"] == [255, 0, 0],
            f"读到 {payload['sanityRed']}",
        )
        check(
            "画了三条线之后整张画布不止一种颜色（说明真画上去了）",
            payload["paintedDistinct"] >= 3,
            f"distinct={payload['paintedDistinct']}",
        )
        check(
            "清空之后整张画布是均匀的（说明上一条不是背景噪声）",
            payload["blankDistinct"] == 1,
            f"distinct={payload['blankDistinct']}",
        )
        print()
        print(f"     自检（画红立刻读）= {payload['sanityRed']}")
        print(f"     三条线的行范围     = {payload['paintedRows']}")
        print(f"     画线后不同颜色数   = {payload['paintedDistinct']}")
        print(f"     清空后不同颜色数   = {payload['blankDistinct']}")
        print(f"     画布后备缓冲       = {payload['canvasSize']}")
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - 兜底
            process.kill()
        shutil.rmtree(profile, ignore_errors=True)

    print()
    print("=" * 84)
    if failures:
        print(f"❌ 失败 {len(failures)} 项：{failures}")
        return 1
    print("✅ 真浏览器里 canvas 能正常光栅化")
    print("=" * 84)
    print()
    print("这一步证明的是**浏览器侧 canvas 可用**；时间轴本身的柱高/柱数/命中")
    print("由 web/test/density.test.mjs 与 tools/verify_timeline_canvas.py 断言。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
