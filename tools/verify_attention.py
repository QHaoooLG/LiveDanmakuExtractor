"""验证「运行中凭据失效提示」端到端：`/api/health` 要带上 `attention`。

需求："当凭证失效时，在启动面板用较为明显的信息栏提示用户登录" ——
启动时那一半由启动面板的登录条覆盖；这里验证**运行中**那一半：
采集失败的原因要能通过 `/api/health` 传到前端横幅。
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bili_danmaku.client import AuthFailed  # noqa: E402
from bili_danmaku.launcher import LauncherOptions, PanelController  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {label}{('  ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


controller = PanelController(LauncherOptions(port=0, out_dir="data"))
try:
    status = controller.start(wait=True, timeout=25)
    if not status.running:
        raise SystemExit(f"面板起不来：{status.error}")
    base = f"http://127.0.0.1:{status.port}"

    def get(path: str) -> dict:
        with urllib.request.urlopen(base + path, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    print("=" * 84)
    print("1. 正常状态：/api/health 带 attention 且为 ok")
    print("=" * 84)
    health = get("/api/health")
    attention = health.get("attention")
    check("health 里有 attention 字段", attention is not None, repr(attention))
    if attention:
        check("没有房间时 level=ok", attention.get("level") == "ok", attention.get("level"))

    print()
    print("=" * 84)
    print("2. 房间正常时也是 ok（单次重连不该报警）")
    print("=" * 84)
    # 加一个房间（真实网络；失败也不影响本用例的判据）
    ok, detail = controller.add_room("804456")
    print(f"  add_room -> {ok} {detail}")
    import time

    time.sleep(2)
    attention = get("/api/health").get("attention") or {}
    print(f"  level={attention.get('level')!r} title={attention.get('title')!r}")
    check("刚加的房间不该立刻报 alert", attention.get("level") != "alert",
          f"level={attention.get('level')}")

    print()
    print("=" * 84)
    print("3. 注入「认证失败」后：attention 必须是 alert 且文案能照做")
    print("=" * 84)
    rooms = controller._panel.rooms() if controller._panel else []
    check("能拿到房间快照", bool(rooms))
    if rooms:
        # 直接改运行时状态：这正是"采集中凭据过期"最终落到快照里的样子
        for runtime in controller._panel._rooms.values():
            runtime.collector._remember_auth_error(AuthFailed(-101), -101)
        attention = get("/api/health").get("attention") or {}
        print(f"  level  = {attention.get('level')!r}")
        print(f"  title  = {attention.get('title')!r}")
        print(f"  detail = {attention.get('detail')!r}")
        check("level 升级为 alert", attention.get("level") == "alert")
        check("标题提到登录", "登录" in str(attention.get("title")))
        check("详情可照做（含扫码/重新登录）",
              any(k in str(attention.get("detail")) for k in ("扫码", "重新登录")))
        check("列出了受影响房间", bool(attention.get("rooms")))

        # 清掉，确认恢复
        for runtime in controller._panel._rooms.values():
            runtime.collector.stats.last_error = ""
            runtime.collector.stats.last_error_kind = ""
        attention = get("/api/health").get("attention") or {}
        check("清掉原因后回到 ok（不会永远挂着）", attention.get("level") == "ok",
              attention.get("level"))
finally:
    controller.stop()

print()
print("=" * 84)
if failures:
    print(f"❌ 失败 {len(failures)} 项：{failures}")
    raise SystemExit(1)
print("✅ 运行中凭据失效能通过 /api/health 传到前端")
print("=" * 84)
