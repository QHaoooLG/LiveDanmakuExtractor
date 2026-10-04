"""诊断 8：验证修复方案 —— 绕开那个不稳的系统代理。

诊断 5/7 已实测：
  * 系统代理 127.0.0.1:7890 在监听，走它 9.4s，直连 0.7s（慢 13 倍）
  * 代理隧道本身能通（CONNECT 200 + TLS 1.3 + HTTP 200），
    所以失败是**偶发**的 —— 典型的代理不稳，不是 pip 坏
  * venv pip 22.3.1 与全局 pip 26.0.1 的 urllib3 在本环境表现**一致**

本脚本测三件事：
  A. pypi.org 的 DNS 解析（看有没有到本地的透明加速）
  B. 直连（禁用代理）跑真正的 pip 命令 —— 能不能装包
  C. 国内镜像（清华）直连 —— 能不能用

注意：探测方式统一用 `--index-url` + `--proxy ""` 来强制直连。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
BUILD = os.path.join(ROOT, "build")
os.makedirs(BUILD, exist_ok=True)

print("=" * 88)
print("A. DNS 解析", flush=True)
print("=" * 88)
for host in ("pypi.org", "files.pythonhosted.org", "pypi.tuna.tsinghua.edu.cn", "mirrors.aliyun.com"):
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        ips = sorted({item[4][0] for item in infos})
        print(f"  {host:<34} {', '.join(ips[:4])}")
    except Exception as exc:
        print(f"  {host:<34} ❌ {type(exc).__name__}: {exc}")

print()
print("=" * 88)
print("B/C. 真跑 pip 命令（每个 90 秒硬超时）", flush=True)
print("=" * 88)

DEST = os.path.join(BUILD, "pipfix")
os.makedirs(DEST, exist_ok=True)

CASES = [
    (
        "B1 现状：默认（走系统代理）",
        ["-m", "pip", "download", "pystray", "--no-deps", "-d", DEST],
        {},
    ),
    (
        "B2 禁用代理：--proxy \"\"",
        ["-m", "pip", "download", "pystray", "--no-deps", "-d", DEST, "--proxy", ""],
        {},
    ),
    (
        "B3 禁用代理 + 清华镜像",
        [
            "-m", "pip", "download", "pystray", "--no-deps", "-d", DEST,
            "--proxy", "", "-i", "https://pypi.tuna.tsinghua.edu.cn/simple",
        ],
        {},
    ),
    (
        "B4 禁用代理 + 环境变量 NO_PROXY",
        ["-m", "pip", "download", "pystray", "--no-deps", "-d", DEST, "--proxy", ""],
        {"NO_PROXY": "*", "no_proxy": "*"},
    ),
]

results = []
for label, args, extra_env in CASES:
    env = dict(os.environ)
    env.update(extra_env)
    env["PYTHONIOENCODING"] = "utf-8"
    print(f"\n【{label}】", flush=True)
    t0 = time.time()
    try:
        result = subprocess.run(
            [VENV_PY, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            env=env,
        )
        elapsed = time.time() - t0
        text = ((result.stdout or "") + (result.stderr or "")).strip()
        lowered = text.lower()
        ok = result.returncode == 0 and "saved" in lowered or "downloading" in lowered
        interesting = [
            line.strip()
            for line in text.splitlines()
            if any(token in line.lower() for token in ("saved", "downloading", "error", "ssl", "could not", "successfully"))
        ]
        status = "✅ 成功" if result.returncode == 0 else "❌ 失败"
        print(f"    {status}  rc={result.returncode}  {elapsed:.1f}s")
        for line in interesting[:6]:
            print(f"      {line[:170]}")
        results.append((label, result.returncode, elapsed, text[:300]))
    except subprocess.TimeoutExpired:
        elapsed = time.time() - t0
        print(f"    ⏱ 90 秒硬超时（挂住）")
        results.append((label, "TIMEOUT", elapsed, ""))

print()
print("=" * 88)
print("汇总")
print("=" * 88)
for label, rc, elapsed, _ in results:
    print(f"  {label:<34} rc={str(rc):<8} {elapsed:5.1f}s")
print()
print("  产物：")
for item in sorted(os.listdir(DEST)):
    print(f"    {item}")
