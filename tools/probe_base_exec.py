"""探针 3：把"绕开 venv stub"方案验证到底。

探针 1/2 已得出的结论（实测）：
  * 不是 Job Object —— 每一层都在 job 里，且 job **允许** breakaway（不解决问题）。
  * 真凶是**多余的 console**：venv 的 `python.exe` 是转发器 stub，
    它起的 base 解释器是 console 程序 -> Windows 分配**新的 conhost**
    （实测 conhost.exe 的 ppid == 真 server 的 pid）。
    关掉那个 console 窗口 = 杀掉挂在它上面的进程。
  * `CREATE_NO_WINDOW` 消不掉这个 conhost（stub 仍会建自己的 console）。

本探针回答：**绕开 stub 之后，import 能不能正常工作？**
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

# 子进程做的事：报告 sys.executable / sys.prefix / sys.path 前几项，
# 然后**真的 import** 项目与依赖 —— 这是"绕开 stub 可行不可行"的判据。
CHILD_SOURCE = r"""
import json, sys, os
info = {
    "executable": sys.executable,
    "prefix": sys.prefix,
    "base_prefix": sys.base_prefix,
    "sys_path_head": sys.path[:4],
    "cwd": os.getcwd(),
}
try:
    import bili_danmaku
    info["import_bili_danmaku"] = os.path.dirname(bili_danmaku.__file__)
except Exception as exc:
    info["import_bili_danmaku"] = f"FAIL {type(exc).__name__}: {exc}"
for mod in ("fastapi", "uvicorn", "httpx", "websockets", "click", "pydantic", "brotli"):
    try:
        __import__(mod)
        info[f"import_{mod}"] = "ok"
    except Exception as exc:
        info[f"import_{mod}"] = f"FAIL {type(exc).__name__}: {exc}"
print("PROBE_RESULT " + json.dumps(info, ensure_ascii=False))
"""

child_py = os.path.join(ROOT, "build", "probe_child.py")
os.makedirs(os.path.dirname(child_py), exist_ok=True)
with open(child_py, "w", encoding="utf-8") as handle:
    handle.write(CHILD_SOURCE)


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


def run_case(label: str, exe: str, *, env: dict | None, extra_flags: int = 0) -> dict:
    """跑一次子进程（**前台等待**，因为要拿它的 stdout 判断 import）。"""
    log = os.path.join(ROOT, "build", f"probe3_{label}.log")
    argv = [exe, child_py]
    flags = 0x00000008 | 0x00000200 | extra_flags  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    with open(log, "a", encoding="utf-8") as handle:
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
            return {"label": label, "error": f"{type(exc).__name__}: {exc}"}
    # 采进程树（子进程此刻正在跑）
    tree = []
    conhost = 0
    for _ in range(40):
        time.sleep(0.25)
        table = snapshot()
        tree = []
        conhost = 0
        frontier = [proc.pid]
        seen = {proc.pid}
        while frontier:
            current = frontier.pop()
            for pid, (ppid, exe_name) in table.items():
                if ppid == current and pid not in seen:
                    seen.add(pid)
                    tree.append({"pid": pid, "ppid": ppid, "exe": exe_name})
                    if exe_name.lower() == "conhost.exe":
                        conhost += 1
                    frontier.append(pid)
        if conhost:
            break
        if proc.poll() is not None:
            break
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
    time.sleep(0.4)
    text = ""
    try:
        with open(log, "r", encoding="utf-8") as reader:
            text = reader.read()
    except OSError:
        pass
    payload = None
    for line in text.splitlines():
        if line.startswith("PROBE_RESULT "):
            payload = line[len("PROBE_RESULT ") :]
    return {
        "label": label,
        "pid": proc.pid,
        "python_levels": len([n for n in tree if n["exe"].lower() == "python.exe"]),
        "conhost": conhost,
        "tree": tree,
        "raw_tail": "\n".join(text.strip().splitlines()[-3:]),
        "payload": payload,
    }


def import_report(payload: str | None) -> str:
    if not payload:
        return "（子进程没有输出 PROBE_RESULT）"
    import json

    try:
        data = json.loads(payload)
    except ValueError:
        return payload
    lines = []
    for key, value in data.items():
        if key.startswith("import_"):
            mark = "✅" if value == "ok" or not str(value).startswith("FAIL") else "❌"
            lines.append(f"    {mark} {key[7:]:<20} {value}")
    lines.insert(0, f"    executable = {data.get('executable')}")
    lines.insert(1, f"    prefix     = {data.get('prefix')}")
    return "\n".join(lines)


def main() -> int:
    venv_exe = sys.executable
    base_exe = getattr(sys, "_base_executable", venv_exe)
    venv_root = os.path.abspath(os.path.join(os.path.dirname(venv_exe), ".."))
    venv_site = os.path.join(venv_root, "Lib", "site-packages")
    src = os.path.join(ROOT, "src")

    env_wrapped = dict(os.environ)
    env_wrapped["PYTHONPATH"] = os.pathsep.join([src, venv_site])
    env_wrapped["VIRTUAL_ENV"] = venv_root
    env_wrapped["PYTHONNOUSERSITE"] = "1"

    print("=" * 78)
    print("方案对比（重点：绕开 stub 之后 import 是否正常）")
    print("=" * 78)

    cases = [
        ("A_现状_stub", venv_exe, dict(os.environ), 0),
        ("B_stub_NO_WINDOW", venv_exe, dict(os.environ), 0x08000000),
        ("C_base_PYTHONPATH", base_exe, env_wrapped, 0),
        ("D_base_裸环境", base_exe, dict(os.environ), 0),
    ]
    results = []
    for label, exe, env, extra in cases:
        print(f"\n【{label}】exe={exe}")
        item = run_case(label, exe, env=env, extra_flags=extra)
        results.append(item)
        if "error" in item:
            print(f"  ❌ {item['error']}")
            continue
        print(f"  进程树里的 python.exe 层数 = {item['python_levels']}，多余 conhost = {item['conhost']}")
        for node in item["tree"]:
            print(f"    pid={node['pid']:<8} ppid={node['ppid']:<8} {node['exe']}")
        print("  import 结果：")
        print(import_report(item.get("payload")))

    print()
    print("=" * 78)
    print("汇总")
    print("=" * 78)
    for item in results:
        if "error" in item:
            print(f"  {item['label']:<20} 启动失败：{item['error']}")
            continue
        ok = item.get("payload") and "FAIL" not in item["payload"]
        print(
            f"  {item['label']:<20} python层数={item['python_levels']} "
            f"多余conhost={item['conhost']} import={'全通' if ok else '有失败'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
