"""一次性探针：查实 `web start -> venv stub -> 真 server` 的进程关系。

回答四个问题（关于"关掉终端后托盘面板是否还活着"的排查清单）：

1. venv 的 `python.exe` 是不是转发器 stub（会再起一个 base 解释器）？
2. 每一层进程是否在 **Job Object** 里？job 是否带 `KILL_ON_JOB_CLOSE`？
3. 每一层是否有 **console window**（决定"关掉那个终端是否连坐"）？
4. 顶层 `Popen` 加 `CREATE_BREAKAWAY_FROM_JOB` 能否成功（失败要能降级）？

只读探测：不改项目代码，只起子进程再枚举。
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from bili_danmaku.runtime import detached_flags  # noqa: E402

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

# --------------------------------------------------------------------------- #
# 1. 枚举进程树（Toolhelp32）
# --------------------------------------------------------------------------- #


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


TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def snapshot() -> dict[int, tuple[int, str]]:
    """pid -> (ppid, exe)。"""
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE:
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


def descendants(root_pid: int, table: dict[int, tuple[int, str]]) -> list[int]:
    """root_pid 的全部子孙（BFS）。"""
    found: list[int] = []
    frontier = [root_pid]
    while frontier:
        current = frontier.pop()
        for pid, (ppid, _) in table.items():
            if ppid == current and pid not in found and pid != root_pid:
                found.append(pid)
                frontier.append(pid)
    return found


def command_line(pid: int) -> str:
    """用 WMI 取命令行（比读 PEB 简单）。"""
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        return (result.stdout or "").strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return f"<查询失败 {type(exc).__name__}>"


# --------------------------------------------------------------------------- #
# 2. Job Object 查询
# --------------------------------------------------------------------------- #

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

JobObjectBasicLimitInformation = 2
JobObjectExtendedLimitInformation = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def job_of(pid: int) -> dict:
    """查 pid 所在的 job（Windows 8+ 嵌套 job 只返回**直接** job）。"""
    result: dict = {"in_job": None, "error": None, "limit_flags": None, "limits": []}
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        result["error"] = f"OpenProcess 失败 err={ctypes.get_last_error()}"
        return result
    try:
        in_job = wintypes.BOOL()
        ok = k32.IsProcessInJob(handle, None, ctypes.byref(in_job))
        if not ok:
            result["error"] = f"IsProcessInJob 失败 err={ctypes.get_last_error()}"
            return result
        result["in_job"] = bool(in_job.value)
        if not result["in_job"]:
            return result
        # 只能拿到 job 的句柄：用 QueryInformationJobObject 需要 job handle。
        # 变通：自己创建一个 job，把本进程放进去（SILENT_BREAKAWAY_OK），
        # 再查本进程的 job —— 这样能枚举出"祖先 job"的 pid 列表。
        result["limits"] = query_nested_jobs(pid)
    finally:
        k32.CloseHandle(handle)
    return result


def query_nested_jobs(pid: int) -> list[dict]:
    """在有嵌套 job 时枚举 pid 的 job 链（Windows 8+）。

    做法：新开一个带 SILENT_BREAKAWAY_OK 的 job、把自己放进去，
    此时 IsProcessInJob(NULL) 返回**新** job；不再有用。
    所以这里改用 **JobObjectBasicProcessIdList** 逐层查询：
    我们没有 job 句柄，只能对"能打开的 job"查询 —— 而 job 句柄无法凭空获得。
    结论：不引入外部工具时只能确认"在 job 里"，拿不到 job 的限制位。
    这里返回空列表并说明原因。
    """
    return []


def console_window(pid: int) -> str:
    """是否有 console window（GetConsoleWindow 只对"本进程"有效，所以枚举窗口标题）。

    用 AttachConsole 尝试附加：成功说明该进程**有 console**。
    """
    ATTACH_PARENT_PROCESS = 0xFFFFFFFF
    # 直接 AttachConsole(pid)：成功 => 该 pid 有 console
    ok = k32.AttachConsole(wintypes.DWORD(pid))
    if ok:
        k32.FreeConsole()
        return "has-console"
    err = ctypes.get_last_error()
    return f"no-console(err={err})"


def is_console_window_visible(pid: int) -> str:
    """枚举顶层窗口，找属于 pid 的可见窗口（stub 的 console 窗口会在这里出现）。"""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    found: list[str] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):  # type: ignore[no-untyped-def]
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            found.append(f"{buf.value!r} [{cls.value}]")
        return True

    user32.EnumWindows(callback, 0)
    return ", ".join(found) if found else "（无可见顶层窗口）"


# --------------------------------------------------------------------------- #
# 3. 主流程
# --------------------------------------------------------------------------- #


def main() -> int:
    print("=" * 78)
    print("A. 本进程（探针本身）")
    print("=" * 78)
    print(f"  pid={os.getpid()}  sys.executable={sys.executable}")
    print(f"  sys._base_executable={getattr(sys, '_base_executable', None)}")
    print(f"  detached_flags()={detached_flags():#010x}")
    print(f"  job={job_of(os.getpid())}")

    # ---- 用 venv stub（现状）起一个子进程 ----
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".runtime", "probe_stub.log")
    log_path = os.path.abspath(log_path)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    sleeper = ["-c", "import time,sys; sys.stdout.write('up\\n'); sys.stdout.flush(); time.sleep(25)"]

    print()
    print("=" * 78)
    print("B. 用 sys.executable（venv stub）分离启动一个 25 秒探针")
    print("=" * 78)
    with open(log_path, "a", encoding="utf-8") as handle:
        proc = subprocess.Popen(
            [sys.executable, *sleeper],
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=detached_flags(),
            close_fds=True,
        )
    print(f"  Popen.pid = {proc.pid}")
    time.sleep(3.0)

    table = snapshot()
    kids = descendants(proc.pid, table)
    print(f"  子进程列表（第一层）: {[(p, table[p][1]) for p in kids]}")
    print()
    for pid in [proc.pid, *kids]:
        ppid, exe = table.get(pid, (0, "?"))
        info = job_of(pid)
        print(f"  pid={pid:<8} ppid={ppid:<8} exe={exe}")
        print(f"      job={info}")
        print(f"      console={console_window(pid)}")
        print(f"      visible_windows={is_console_window_visible(pid)}")
        cmd = command_line(pid)
        print(f"      cmdline={(cmd[:150] + '...') if len(cmd) > 150 else cmd}")

    # 收尾
    for pid in [proc.pid, *kids]:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)

    # ---- 试 CREATE_BREAKAWAY_FROM_JOB ----
    print()
    print("=" * 78)
    print("C. 顶层 Popen 加 CREATE_BREAKAWAY_FROM_JOB")
    print("=" * 78)
    CREATE_BREAKAWAY_FROM_JOB = 0x01000000
    try:
        with open(log_path, "a", encoding="utf-8") as handle:
            proc2 = subprocess.Popen(
                [sys.executable, *sleeper],
                stdout=handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=detached_flags() | CREATE_BREAKAWAY_FROM_JOB,
                close_fds=True,
            )
        print(f"  ✅ 成功：Popen.pid = {proc2.pid}（说明当前 job 允许 breakaway）")
        time.sleep(2.0)
        table2 = snapshot()
        kids2 = descendants(proc2.pid, table2)
        print(f"  子孙: {[(p, table2[p][1]) for p in kids2]}")
        for pid in [proc2.pid, *kids2]:
            print(f"      pid={pid} job={job_of(pid)}")
        for pid in [proc2.pid, *kids2]:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
    except OSError as exc:
        print(f"  ❌ 失败：{type(exc).__name__}: {exc}")
        print("     => 说明当前 job 不允许 breakaway，任何方案都必须带降级路径")

    print()
    print("=" * 78)
    print("D. 结论")
    print("=" * 78)
    print(json.dumps({"probe_pid": os.getpid(), "stub_pid": proc.pid}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
