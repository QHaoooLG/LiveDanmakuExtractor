"""探针 6：用**真的 CLI** 验证「绕开 stub 启动 panel」端到端可行。

模拟 runtime.server_argv() 改造后会做的事：
  base 解释器 + `-m bili_danmaku web ...` + PYTHONPATH(src + venv site-packages)

检查：
  1. 进程树里没有多余的 conhost（顶层进程就是 server）；
  2. 状态文件里记录的 pid == 顶层 pid（stop_runtime 依赖它）；
  3. HTTP /api/health 能通；
  4. `web stop`（优雅停止）依然能把它收掉。
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

PORT = 18789
STATE_DIR = os.path.join(ROOT, ".runtime_probe")

k32 = ctypes.WinDLL("kernel32", use_last_error=True)


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
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


def tree_of(root: int) -> list[tuple[int, int, str]]:
    table = snapshot()
    out: list[tuple[int, int, str]] = []
    frontier, seen = [root], {root}
    while frontier:
        current = frontier.pop()
        for pid, (ppid, exe) in table.items():
            if ppid == current and pid not in seen:
                seen.add(pid)
                out.append((pid, ppid, exe))
                frontier.append(pid)
    return out


def build_env() -> dict:
    venv_exe = sys.executable
    venv_root = os.path.abspath(os.path.join(os.path.dirname(venv_exe), ".."))
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [os.path.join(ROOT, "src"), os.path.join(venv_root, "Lib", "site-packages")]
    )
    env["VIRTUAL_ENV"] = venv_root
    env["PYTHONNOUSERSITE"] = "1"
    env.pop("PYTHONHOME", None)
    return env


def http_get(path: str, timeout: float = 5.0) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=timeout) as response:
            return True, response.read().decode("utf-8", "replace")[:300]
    except (urllib.error.URLError, OSError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


def main() -> int:
    exe = getattr(sys, "_base_executable", sys.executable)
    env = build_env()
    state_file = os.path.join(STATE_DIR, "web.json")
    os.makedirs(STATE_DIR, exist_ok=True)

    print("=" * 78)
    print("用 base 解释器启动**真的** panel（不经过 venv stub）")
    print("=" * 78)
    print(f"  exe        = {exe}")
    print(f"  PYTHONPATH = {env['PYTHONPATH']}")

    # 注意：这里用 --config 指向不存在的路径会被拒绝，所以只传端口与 --out。
    argv = [
        exe,
        "-m",
        "bili_danmaku",
        "web",
        "_serve",
        "--managed",
        "--port",
        str(PORT),
        "--out",
        os.path.join(ROOT, "data_probe"),
    ]
    log_path = os.path.join(ROOT, "build", "probe6_panel.log")
    with open(log_path, "a", encoding="utf-8") as handle:
        proc = subprocess.Popen(
            argv,
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=0x00000008 | 0x00000200,
            close_fds=True,
            env=env,
            cwd=ROOT,
        )
    print(f"  Popen.pid  = {proc.pid}")

    # 等 HTTP 起来
    healthy = False
    for _ in range(40):
        time.sleep(0.5)
        ok, body = http_get("/api/health")
        if ok:
            healthy = True
            print(f"  /api/health = {body}")
            break
        if proc.poll() is not None:
            print(f"  ❌ 进程已退出，returncode={proc.returncode}")
            break
    else:
        print("  ❌ 30 秒内没起来")

    nodes = tree_of(proc.pid)
    conhosts = [n for n in nodes if n[2].lower() == "conhost.exe"]
    print(f"  进程树     = {nodes}")
    print(f"  多余 conhost = {[c[0] for c in conhosts]}  -> {'❌ 仍然有' if conhosts else '✅ 无（顶层就是 server）'}")
    print(f"  顶层 pid 就是 server = {'✅' if not conhosts else '❌'}")

    # 状态文件里的 pid
    state = os.path.join(ROOT, ".runtime", "web.json")
    if os.path.isfile(state):
        payload = json.loads(open(state, encoding="utf-8").read())
        print(f"  .runtime/web.json pid = {payload.get('pid')}（端口 {payload.get('port')}）")

    # 优雅停止
    print()
    print("  优雅停止（POST /api/admin/shutdown，等价于 web stop）…")
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/api/admin/shutdown",
        method="POST",
        data=b"",
    )
    request.add_header("Origin", f"http://127.0.0.1:{PORT}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            print(f"  shutdown -> HTTP {response.status} {response.read().decode('utf-8', 'replace')[:120]}")
    except (urllib.error.URLError, OSError) as exc:
        print(f"  shutdown 请求失败：{type(exc).__name__}: {exc}")

    for _ in range(40):
        time.sleep(0.5)
        if proc.poll() is not None:
            print(f"  ✅ server 已优雅退出，returncode={proc.returncode}")
            break
    else:
        print("  ⚠️ 10 秒内没退出，强杀")
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)

    for pid, _, _ in nodes:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False)

    print()
    print("  日志尾部：")
    try:
        with open(log_path, encoding="utf-8") as reader:
            for line in reader.read().strip().splitlines()[-8:]:
                print(f"    {line}")
    except OSError:
        pass
    print()
    print(f"  healthy={healthy}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
