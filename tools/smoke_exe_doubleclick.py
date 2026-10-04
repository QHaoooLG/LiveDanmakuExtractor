"""验证 exe 的**双击行为**：不带参数 = 启动托盘 + 启动面板。

`--windowed` 的 exe 没法用 stdin/stdout 交互，所以这里用"外部观察"验证：
  1. 不带参数启动它（模拟双击）；
  2. 轮询进程列表，确认有 `B站弹幕提取姬.exe` 在跑，且**没有**多出来的 conhost
     （这正是 §5.12 那个缺陷在新入口上消失的证据）；
  3. 轮询 8787 端口（配置里的默认端口），确认面板真的起来了；
  4. 确认托盘消息窗口存在（按窗口标题找）；
  5. 收尾：强杀（因为是 GUI 进程，走不到优雅停止路径；只验证启动行为）。
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
EXE = ROOT / "dist" / "exe" / "B站弹幕提取姬.exe"

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)


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


def visible_windows() -> list[tuple[int, str, str]]:
    """枚举所有可见顶层窗口：(pid, 标题, 类名)。"""
    found: list[tuple[int, str, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if buf.value or cls.value:
            found.append((owner.value, buf.value, cls.value))
        return True

    user32.EnumWindows(callback, 0)
    return found


def find_message_window(class_prefix: str) -> int | None:
    """找消息专用窗口。

    `HWND_MESSAGE` 建的窗口**不会**被 `EnumWindows` 枚举到（那是设计如此），
    但可以用 `FindWindowExW` 从 `HWND_MESSAGE` 这棵子树里按类名找。
    """
    HWND_MESSAGE = wintypes.HWND(-3)
    hwnd = user32.FindWindowExW(HWND_MESSAGE, None, None, None)
    seen = 0
    while hwnd and seen < 200:
        seen += 1
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if cls.value.startswith(class_prefix):
            return int(hwnd)
        hwnd = user32.FindWindowExW(HWND_MESSAGE, hwnd, None, None)
    return None


def health(port: int, timeout: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {label}{('  ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


print("=" * 84)
print("模拟双击 exe（不带任何参数）")
print("=" * 84)
print(f"  目标 = {EXE}")
if not EXE.is_file():
    print("  ❌ exe 不存在，请先 build_exe.py")
    raise SystemExit(1)

log_path = ROOT / ".runtime" / "web.log"
log_size_before = log_path.stat().st_size if log_path.is_file() else 0

# 两种启动方式都要验，因为它们的预期**不同**（DEC-6 / D4）：
#   A. 不带参数（= 双击）：只起托盘 + 启动面板，**不自动开始采集** → 面板不监听
#   B. `tray --rooms <房间>`：起托盘并真的把面板跑起来 → 应能访问 /api/health
MODE = os.environ.get("SMOKE_MODE", "B")
if MODE == "A":
    argv = [str(EXE)]
    expect_panel = False
    label = "A. 双击（不带参数）—— 不自动开始采集"
else:
    argv = [str(EXE), "tray", "--rooms", "804456", "--no-open"]
    expect_panel = True
    label = "B. tray --rooms 804456 —— 面板应起来"

print()
print("=" * 84)
print(f"启动方式：{label}")
print("=" * 84)
print(f"  命令 = {' '.join(argv)}")

# --windowed 的 GUI 进程：模拟 Explorer 双击（分离、不继承控制台）
proc = subprocess.Popen(
    argv,
    cwd=str(ROOT),
    creationflags=0x00000008 | 0x00000200,
    close_fds=True,
)
print(f"  已启动 pid={proc.pid}")

panel_port = None
payload = None
try:
    deadline = time.time() + 40
    while time.time() < deadline:
        for port in (8787, 8788, 8789, 8790, 8791):
            data = health(port)
            if data:
                panel_port, payload = port, data
                break
        if panel_port:
            break
        if proc.poll() is not None:
            print(f"  ❌ exe 已退出，returncode={proc.returncode}")
            break
        time.sleep(1.0)
    # 给 A 模式一点时间确认"它确实不监听"
    if not expect_panel and proc.poll() is None:
        time.sleep(3.0)

    print()
    print("=" * 84)
    print("观察结果")
    print("=" * 84)

    check("exe 进程仍在运行（托盘常驻）", proc.poll() is None)
    if expect_panel:
        check("面板已在监听", panel_port is not None, f"端口 {panel_port}")
        if payload:
            check("frontend_built=True", payload.get("frontend_built") is True)
            check("content_only=True", payload.get("content_only") is True)
    else:
        check("按设计**不**自动开始采集（面板未监听）", panel_port is None,
              "" if panel_port is None else f"却在 {panel_port} 监听了")

    table = snapshot()
    # PyInstaller onefile 的引导器会再起一个自己（真进程），树里有 1-2 个同名进程
    kids = descendants(proc.pid, table)
    child_names = [table[p][1] for p in kids]
    conhosts = [n for n in child_names if n.lower() == "conhost.exe"]
    pythons = [n for n in child_names if "python" in n.lower()]
    print(f"     子进程 = {[(p, table[p][1]) for p in kids]}")
    check("没有多余的 conhost（§5.12 的缺陷在新入口消失）", not conhosts)
    check("没有 python 子进程（面板在托盘进程内）", not pythons)

    # 窗口归属：真实进程可能是引导器的子进程（onefile 会 fork 自己）
    candidate_pids = {proc.pid, *kids}
    windows = visible_windows()
    mine = [(title, cls) for pid, title, cls in windows if pid in candidate_pids]
    print(f"     本进程可见窗口 = {mine}")
    check("启动面板窗口已显示", any("启动面板" in title for title, _ in mine))
    # 托盘的是**消息专用窗口**（HWND_MESSAGE），不会出现在 EnumWindows 里，
    # 所以改用"消息专用窗口能否被 FindWindowEx 找到"来判断。
    message_only = find_message_window("BiliDanmakuTray")
    check("托盘消息专用窗口存在", message_only is not None,
          f"hwnd={message_only}" if message_only else "未找到（托盘未挂上？）")

    log_size_after = log_path.stat().st_size if log_path.is_file() else 0
    check("日志文件已建立（windowed 的 stdout 重定向生效）", log_path.is_file())
    print(f"     日志大小 {log_size_before} → {log_size_after} 字节")
finally:
    print()
    print("  收尾：强杀 GUI 进程（本脚本只验证启动行为）")
    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
    for pid in descendants(proc.pid, snapshot()):
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False)
    time.sleep(0.5)

print()
print("=" * 84)
if failures:
    print(f"❌ 失败 {len(failures)} 项：{failures}")
    raise SystemExit(1)
print("✅ 启动行为符合预期")
print("=" * 84)
