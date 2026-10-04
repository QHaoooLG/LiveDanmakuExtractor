"""探针 2：对比几种"让面板活过终端"的启动方式。

探针 1 已更正了一个文档里的假设：
  * 不是 Job Object 的问题（每一层都在 job 里，且 job **允许** breakaway）；
  * 真凶是 **多出来的一层 console**：venv 的 `python.exe`（转发器 stub）会再起
    一个 base 解释器，而那个 base 解释器是 console 程序 -> Windows 给它分配了
    **一个新的 conhost**（探针 1 实测：`conhost.exe` 的 ppid == 真 server 的 pid，
    且真 server 有一个 `PseudoConsoleWindow` 类窗口）。

本探针量化对比 4 种启动方式的进程树形态，并验证"绕开 stub"是否可行。
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

from bili_danmaku.runtime import detached_flags  # noqa: E402

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


def descendants(root: int, table: dict[int, tuple[int, str]]) -> list[int]:
    found: list[int] = []
    frontier = [root]
    while frontier:
        current = frontier.pop()
        for pid, (ppid, _) in table.items():
            if ppid == current and pid not in found and pid != root:
                found.append(pid)
                frontier.append(pid)
    return found


SLEEPER = ["-c", "import time,sys; sys.stdout.write('up\\n'); sys.stdout.flush(); time.sleep(20)"]


def spawn(label: str, argv: list[str], *, flags: int, env: dict | None = None) -> dict:
    log = os.path.join(ROOT, "build", f"probe_{label}.log")
    os.makedirs(os.path.dirname(log), exist_ok=True)
    handle = open(log, "a", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            argv,
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
            env=env,
        )
    except OSError as exc:
        handle.close()
        return {"label": label, "error": f"{type(exc).__name__}: {exc}"}
    handle.close()
    time.sleep(3.0)
    table = snapshot()
    kids = descendants(proc.pid, table)
    conhosts = [p for p in kids if table.get(p, (0, ""))[1].lower() == "conhost.exe"]
    logtext = ""
    try:
        with open(log, "r", encoding="utf-8") as reader:
            logtext = reader.read().strip().replace("\n", " | ")[:200]
    except OSError:
        pass
    result = {
        "label": label,
        "pid": proc.pid,
        "tree": [
            {"pid": p, "ppid": table.get(p, (0, "?"))[0], "exe": table.get(p, (0, "?"))[1]}
            for p in [proc.pid, *kids]
        ],
        "levels": 1 + len([p for p in kids if table.get(p, (0, ""))[1].lower() != "conhost.exe"]),
        "conhost_count": len(conhosts),
        "log": logtext,
    }
    for pid in [proc.pid, *kids]:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
    time.sleep(0.5)
    return result


def main() -> int:
    venv_exe = sys.executable
    base_exe = getattr(sys, "_base_executable", venv_exe)
    venv_site = os.path.join(os.path.dirname(venv_exe), "..", "Lib", "site-packages")
    venv_site = os.path.abspath(venv_site)
    src = os.path.join(ROOT, "src")

    # 现成的一份"干净"环境副本，用来重定向 PYTHONPATH（包含 venv 的 site-packages）
    env_base = dict(os.environ)
    env_wrapped = dict(os.environ)
    env_wrapped["PYTHONPATH"] = os.pathsep.join([src, venv_site])
    env_wrapped["PYTHONNOUSERSITE"] = "1"
    env_wrapped["VIRTUAL_ENV"] = os.path.abspath(os.path.join(os.path.dirname(venv_exe), ".."))

    print("=" * 78)
    print("环境事实")
    print("=" * 78)
    print(f"  venv_exe  = {venv_exe}  ({os.path.getsize(venv_exe)} bytes)")
    print(f"  base_exe  = {base_exe}  (exists={os.path.isfile(base_exe)}, {os.path.getsize(base_exe) if os.path.isfile(base_exe) else 0} bytes)")
    print(f"  venv_site = {venv_site}  (exists={os.path.isdir(venv_site)})")
    print(f"  src       = {src}")

    results = []

    print()
    print("=" * 78)
    print("① 现状：venv stub + DETACHED_PROCESS|NEW_PROCESS_GROUP")
    print("=" * 78)
    results.append(spawn("stub_detached", [venv_exe, *SLEEPER], flags=detached_flags()))

    print()
    print("=" * 78)
    print("② venv stub + CREATE_NO_WINDOW（不给它 console）")
    print("=" * 78)
    CREATE_NO_WINDOW = 0x08000000
    results.append(spawn("stub_nowindow", [venv_exe, *SLEEPER], flags=detached_flags() | CREATE_NO_WINDOW))

    print()
    print("=" * 78)
    print("③ 绕开 stub：base 解释器 + PYTHONPATH(VIRTUAL_ENV/site-packages) + DETACHED")
    print("=" * 78)
    results.append(spawn("base_detached", [base_exe, *SLEEPER], flags=detached_flags(), env=env_wrapped))

    print()
    print("=" * 78)
    print("④ base 解释器 + DETACHED|NEW_PROCESS_GROUP 不带 PYTHONPATH（对照，应失败）")
    print("=" * 78)
    results.append(spawn("base_nopath", [base_exe, *SLEEPER], flags=detached_flags(), env=env_base))

    print()
    print("=" * 78)
    print("进程树对比")
    print("=" * 78)
    for item in results:
        if "error" in item:
            print(f"\n【{item['label']}】❌ {item['error']}")
            continue
        print(f"\n【{item['label']}】pid={item['pid']} 层数(不含conhost)={item['levels']} conhost={item['conhost_count']}")
        for node in item["tree"]:
            marker = "  <-- 多余的 console" if node["exe"].lower() == "conhost.exe" else ""
            print(f"    pid={node['pid']:<8} ppid={node['ppid']:<8} {node['exe']}{marker}")
        print(f"    子进程 stdout: {item['log']!r}")

    print()
    print("=" * 78)
    print("结论对照")
    print("=" * 78)
    for item in results:
        if "error" in item:
            print(f"  {item['label']:<16} 启动失败")
            continue
        extra = "有多余 console" if item["conhost_count"] else "无多余 console"
        layers = "三层(有 stub)" if item["levels"] >= 3 else "两层(无 stub)"
        print(f"  {item['label']:<16} {layers:<14} {extra}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
