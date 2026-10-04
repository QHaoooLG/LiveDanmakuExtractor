"""验证 §5.12 的修复：`web start` 起的服务是否**不再有多余 console**。

修法：`runtime.child_python()` 绕开 venv 的转发器 stub，直接起 base 解释器，
并把当前 `sys.path` 注入 `PYTHONPATH`。

判据（不依赖"关终端"这种只能人工做的动作）：
  1. 面板起来了（`web status` / HTTP 可达）；
  2. 进程树里**只有一个** python 进程（顶层就是 server，没有 stub 中转）；
  3. **没有 conhost.exe** 作为 server 的子进程 —— 这正是"多余终端窗口"的来源；
  4. 服务能 import 到本项目（说明 PYTHONPATH 注入有效）。
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = 18899

k32 = ctypes.WinDLL("kernel32", use_last_error=True)


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
    ]


def snapshot() -> dict[int, tuple[int, str]]:
    snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    out: dict[int, tuple[int, str]] = {}
    try:
        if not k32.Process32FirstW(snap, ctypes.byref(entry)):
            return {}
        while True:
            out[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID), str(entry.szExeFile))
            if not k32.Process32NextW(snap, ctypes.byref(entry)):
                break
    finally:
        k32.CloseHandle(snap)
    return out


def descendants(root: int, table: dict[int, tuple[int, str]]) -> list[int]:
    found, frontier = [], [root]
    while frontier:
        current = frontier.pop()
        for pid, (ppid, _) in table.items():
            if ppid == current and pid not in found and pid != root:
                found.append(pid)
                frontier.append(pid)
    return found


failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {label}{('  ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def run(*args: str, timeout: int = 60) -> tuple[int, str]:
    result = subprocess.run(
        [sys.executable, "-m", "bili_danmaku", *args],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout,
    )
    return result.returncode, ((result.stdout or "") + (result.stderr or "")).strip()


print("=" * 84)
print("1. 清理可能存在的旧实例")
print("=" * 84)
code, out = run("web", "stop", timeout=90)
print(f"  web stop rc={code}: {out.splitlines()[-1] if out else ''}")

print()
print("=" * 84)
print(f"2. web start --port {PORT}")
print("=" * 84)
code, out = run("web", "start", "--port", str(PORT))
print(f"  rc={code}")
for line in out.splitlines():
    print(f"    {line}")

state = ROOT / ".runtime" / "web.json"
try:
    pid = json.loads(state.read_text(encoding="utf-8"))["pid"] if state.is_file() else None
except (ValueError, KeyError, OSError):
    pid = None
print(f"  状态文件 pid = {pid}")

if pid is None:
    check("web start 成功", False, "没有拿到状态文件 pid")
else:
    check("web start 成功", code == 0)
    table = snapshot()
    kids = descendants(pid, table)
    names = [table[p][1] for p in kids]
    print(f"  服务进程树（pid={pid}）:")
    print(f"    顶层 = {table.get(pid, (0, '?'))[1]}")
    for p in kids:
        print(f"      └─ pid={p} {table[p][1]}")

    pythons = [n for n in names if "python" in n.lower()]
    conhosts = [n for n in names if n.lower() == "conhost.exe"]

    check("没有中转 python 子进程（绕开了 venv stub）", not pythons,
          f"却发现 {pythons}" if pythons else "")
    check("没有多余的 conhost（§5.12 的窗口来源）", not conhosts,
          f"却发现 {conhosts}" if conhosts else "")

    # 顶层进程的可执行文件必须是 base 解释器（不是 venv 的 stub）
    top_exe = table.get(pid, (0, ""))[1]
    check("顶层进程就是 server", top_exe.lower().startswith("python"), f"顶层 = {top_exe}")

    print()
    print("=" * 84)
    print("3. HTTP 可达（证明 PYTHONPATH 注入有效、import 正常）")
    print("=" * 84)
    ok_http = False
    for _ in range(40):
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{PORT}/api/health", timeout=3
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
            ok_http = True
            break
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(0.5)
    check("GET /api/health", ok_http)
    if ok_http:
        print(f"    version={payload.get('version')} frontend_built={payload.get('frontend_built')}")

    print()
    print("=" * 84)
    print("4. web status / web stop")
    print("=" * 84)
    code_s, out_s = run("web", "status")
    print(f"  status rc={code_s}")
    for line in out_s.splitlines():
        print(f"    {line}")
    check("web status 认出实例", str(pid) in out_s)

    code_t, out_t = run("web", "stop", timeout=90)
    print(f"  stop rc={code_t}: {out_t.splitlines()[-1] if out_t else ''}")
    check("web stop 优雅停止", code_t == 0 and "flush" in out_t)

    # 收尾：确保没有残留
    for p in [pid, *descendants(pid, snapshot())]:
        subprocess.run(["taskkill", "/PID", str(p), "/F"], capture_output=True, check=False)

print()
print("=" * 84)
if failures:
    print(f"❌ 失败 {len(failures)} 项：{failures}")
    raise SystemExit(1)
print("✅ web start 路径的终端连坐缺陷已修复")
print("=" * 84)
