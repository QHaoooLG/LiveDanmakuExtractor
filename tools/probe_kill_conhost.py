"""探针 4：**因果验证** —— 杀掉那个多余的 conhost 是否连坐真 server？

这正是"关掉那个终端窗口"在 Windows 上的等价操作：
关闭 console 窗口 -> conhost 退出 -> 挂在该 console 上的进程收到
CTRL_CLOSE_EVENT（约 5 秒宽限）后被杀。

对照：
  * stub 启动（现状）—— 预期：真 server 跟着死
  * base 解释器启动（无 conhost）—— 预期：不受影响（因为没有 console 可关）
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


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
    if snap == ctypes.c_void_p(-1).value:
        return {}
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


def pid_alive(pid: int) -> bool:
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
        return bool(ok) and code.value == 259
    finally:
        k32.CloseHandle(handle)


def tree_of(root: int) -> list[tuple[int, int, str]]:
    table = snapshot()
    out: list[tuple[int, int, str]] = []
    frontier = [root]
    seen = {root}
    while frontier:
        current = frontier.pop()
        for pid, (ppid, exe) in table.items():
            if ppid == current and pid not in seen:
                seen.add(pid)
                out.append((pid, ppid, exe))
                frontier.append(pid)
    return out


def child_exe_for(mode: str) -> tuple[str, dict | None]:
    venv_exe = sys.executable
    if mode == "stub":
        return venv_exe, None
    base_exe = getattr(sys, "_base_executable", venv_exe)
    venv_root = os.path.abspath(os.path.join(os.path.dirname(venv_exe), ".."))
    src = os.path.join(ROOT, "src")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([src, os.path.join(venv_root, "Lib", "site-packages")])
    env["VIRTUAL_ENV"] = venv_root
    env["PYTHONNOUSERSITE"] = "1"
    return base_exe, env


SLEEP_SOURCE = "import time; time.sleep(60)"


def main() -> int:
    print("=" * 78)
    print("实验：杀掉多余的 conhost（= 关掉终端窗口）后，真 server 是否连坐")
    print("=" * 78)

    for mode in ("stub", "base"):
        label = "① 现状（venv stub）" if mode == "stub" else "② 绕开 stub（base 解释器）"
        exe, env = child_exe_for(mode)
        log = os.path.join(ROOT, "build", f"probe4_{mode}.log")
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(log, "a", encoding="utf-8") as handle:
            proc = subprocess.Popen(
                [exe, "-c", SLEEP_SOURCE],
                stdout=handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=0x00000008 | 0x00000200,
                close_fds=True,
                env=env,
            )
        time.sleep(3.0)
        nodes = tree_of(proc.pid)
        pythons = [p for p in nodes if p[2].lower() == "python.exe"]
        conhosts = [p for p in nodes if p[2].lower() == "conhost.exe"]
        # "真 server" = 进程树里**最深**的 python（stub 模式下是 base 解释器）
        server_pid = pythons[-1][0] if pythons else proc.pid

        print(f"\n{label}")
        print(f"  exe           = {exe}")
        print(f"  Popen.pid     = {proc.pid}")
        print(f"  进程树        = {[(p, pp, e) for p, pp, e in nodes]}")
        print(f"  真 server pid = {server_pid}，多余 conhost = {[c[0] for c in conhosts]}")

        if not conhosts:
            print("  没有多余的 conhost —— 没有 console 窗口可关。")
            print(f"  （跳过杀 conhost；直接清理）")
        else:
            victim = conhosts[0][0]
            print(f"  >>> 杀掉 conhost {victim}（等价于用户关掉那个终端窗口）")
            subprocess.run(["taskkill", "/PID", str(victim), "/F"], capture_output=True, check=False)
            for waited in (1, 2, 3, 5, 8):
                time.sleep(waited if waited == 1 else 1)
                if not pid_alive(server_pid):
                    print(f"  ❌ 真 server {server_pid} 在约 {waited}s 后**已死** —— 终端连坐复现")
                    break
            else:
                print(f"  ✅ 真 server {server_pid} 仍然存活 —— 关 console 没有连坐")

        # 清理（把整棵树带走）
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
        for pid, _, _ in nodes:
            if pid_alive(pid):
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False)
        time.sleep(0.5)

    print()
    print("=" * 78)
    print("注意：本 harness 的 console 是 ConPTY（伪终端），与真人双击开的")
    print("conhost 窗口不完全等价；结论以进程/console 归属关系为准。")
    print("最终验证必须在真人 PowerShell 里做（见 HANDOVER §5.12 的验证步骤）。")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
