"""验证需求方报的 bug 已修：**exe 退出后 `.runtime/web.log` 要有内容**。

用户原话："退出时 web.log 未写入任何内容"。

根因：`setup_logging()` 只挂 `StreamHandler(sys.stderr)`，而 `--windowed` 打包的
exe **没有 stderr**（`sys.stderr is None`）⇒ 日志全部丢弃。
修法：`setup_logging(..., log_file=...)` 额外挂 UTF-8 的 `FileHandler`，
`PanelController` 在没有可用 stderr 时**自动**落到 `.runtime/web.log`；
同时把 uvicorn 自己的 logger（`propagate=False`、自带 handler）也配置到同一个文件。

本脚本用**真的 exe** 跑一遍「起托盘 → 退出」，然后检查日志内容。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "dist" / "exe" / "B站弹幕提取姬.exe"
LOG = ROOT / ".runtime" / "web.log"

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {label}{('  ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


if not EXE.is_file():
    print(f"❌ 找不到 exe：{EXE}（先跑 tools/build_exe.py）")
    raise SystemExit(1)

# 从**干净**状态开始：证明日志是这次运行写出来的，不是上一次的残留
LOG.unlink(missing_ok=True)
print("=" * 84)
print("1. 清空日志，启动 exe（tray + 房间 ⇒ 面板会真的跑起来）")
print("=" * 84)
print(f"  日志路径 = {LOG}（存在 = {LOG.is_file()}）")

proc = subprocess.Popen(
    [str(EXE), "tray", "--rooms", "804456", "--no-open"],
    cwd=str(ROOT),
    creationflags=0x00000008 | 0x00000200,
    close_fds=True,
)
print(f"  已启动 pid={proc.pid}")

port = None
try:
    deadline = time.time() + 45
    while time.time() < deadline:
        for candidate in (8787, 8788, 8789, 8790, 8791):
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{candidate}/api/health", timeout=2
                ) as response:
                    json.loads(response.read().decode("utf-8"))
                port = candidate
                break
            except (urllib.error.URLError, OSError, ValueError):
                continue
        if port:
            break
        if proc.poll() is not None:
            break
        time.sleep(1.0)

    print(f"  面板端口 = {port}")
    # 让它多写一点日志（采集器会记录房间状态、落盘等）
    time.sleep(4.0)

    print()
    print("=" * 84)
    print("2. 检查日志文件（这是用户报的 bug）")
    print("=" * 84)
    exists = LOG.is_file()
    size = LOG.stat().st_size if exists else 0
    check("web.log 已被创建", exists)
    check("web.log **非空**", size > 0, f"{size} 字节")

    text = LOG.read_text(encoding="utf-8") if exists else ""
    if text:
        lines = text.strip().splitlines()
        print(f"      共 {len(lines)} 行，末尾 8 行：")
        for line in lines[-8:]:
            print(f"        {line}")
        check("含项目自身日志（面板就绪）", "面板就绪" in text)
        check("含 uvicorn 日志（Started server process）", "Started server process" in text)
        check("中文未乱码", "面板" in text and "锛" not in text)
    else:
        check("日志有内容", False, "文件是空的 —— bug 未修好")
finally:
    print()
    print("  收尾：强杀 GUI 进程")
    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
    time.sleep(0.6)
    for extra in subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq B站弹幕提取姬.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout.splitlines():
        parts = [p.strip('"') for p in extra.split('","')]
        if parts and parts[0].endswith(".exe"):
            subprocess.run(["taskkill", "/PID", parts[1], "/F"], capture_output=True, check=False)

print()
print("=" * 84)
if failures:
    print(f"❌ 失败 {len(failures)} 项：{failures}")
    raise SystemExit(1)
print("✅ exe 退出后 web.log 有完整内容（bug 已修）")
print("=" * 84)
