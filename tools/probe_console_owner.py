"""探针 5：确定"谁拥有那个 console"。

`AttachConsole(pid)` 成功 == 该 pid **有** console（且我们成功附加上去）。
用"探针自己"做参照系，分别测：
  * 探针自己（harness 给的 ConPTY console）
  * 顶层 Popen（DETACHED_PROCESS 起的那一层）
  * stub 起出来的 base 解释器（真 server）
  * 那个 conhost 的归属
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


def probe_console(pid: int) -> str:
    """AttachConsole(pid)：成功 => 该 pid 有 console。

    注意：**我们自己的 console 会被 FreeConsole 丢掉**，所以每测一个 pid
    都要重新 FreeConsole 再试；失败的错误码含义：
      5  = ERROR_ACCESS_DENIED（目标没有 console，或我们已被附加到别的 console）
      6  = ERROR_INVALID_HANDLE
    """
    k32.FreeConsole()
    ctypes.set_last_error(0)
    ok = k32.AttachConsole(wintypes.DWORD(pid))
    if ok:
        k32.FreeConsole()
        return "有 console ✅"
    err = ctypes.get_last_error()
    return f"无 console（err={err}）"


def main() -> int:
    print("=" * 78)
    print("参照：探针自己")
    print("=" * 78)
    print(f"  pid={os.getpid()}")
    print(f"  {probe_console(os.getpid())}")

    venv_exe = sys.executable
    base_exe = getattr(sys, "_base_executable", venv_exe)
    venv_root = os.path.abspath(os.path.join(os.path.dirname(venv_exe), ".."))
    env_base = dict(os.environ)
    env_base["PYTHONPATH"] = os.pathsep.join(
        [os.path.join(ROOT, "src"), os.path.join(venv_root, "Lib", "site-packages")]
    )
    env_base["VIRTUAL_ENV"] = venv_root
    env_base["PYTHONNOUSERSITE"] = "1"

    for mode, exe, env in (
        ("现状 stub", venv_exe, None),
        ("绕开 stub（base）", base_exe, env_base),
    ):
        print()
        print("=" * 78)
        print(f"{mode}：exe={exe}")
        print("=" * 78)
        log = os.path.join(ROOT, "build", f"probe5_{'stub' if env is None else 'base'}.log")
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(log, "a", encoding="utf-8") as handle:
            proc = subprocess.Popen(
                [exe, "-c", "import time; time.sleep(30)"],
                stdout=handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=0x00000008 | 0x00000200,
                close_fds=True,
                env=env,
            )
        time.sleep(3.0)
        nodes = tree_of(proc.pid)
        print(f"  Popen.pid = {proc.pid}")
        for pid, ppid, exe_name in [(proc.pid, 0, "(顶层)"), *nodes]:
            role = {
                "conhost.exe": "console 宿主",
                "python.exe": "python",
            }.get(exe_name.lower(), exe_name)
            print(f"  pid={pid:<8} ppid={ppid:<8} {exe_name:<14} {role:<12} {probe_console(pid)}")
        # 清理
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
        for pid, _, _ in nodes:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False)
        time.sleep(0.5)

    print()
    print("=" * 78)
    print("读法：")
    print("  * 顶层 Popen（DETACHED_PROCESS）没有 console —— 说明 detach 本身是生效的。")
    print("  * 若真 server 有 console，而它是被 stub 间接启动的，")
    print("    那个 console 就是用户看到的窗口，关它必然连坐。")
    print("  * base 解释器直接启动时**没有** 中间层，也就没有额外 console。")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
