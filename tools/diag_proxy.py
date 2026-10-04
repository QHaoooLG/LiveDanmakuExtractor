"""诊断 5：分辨"直连"与"走代理"两条路径，并请出 urllib3 的调试日志。

已知：
  * `socket.create_connection(("pypi.org", 443))` 直连 -> TLS 1.3 成功
  * `urllib.request.getproxies()` -> {'https': 'http://127.0.0.1:7890'}
    （这是**注册表**里的系统代理，urllib/urllib3/pip 都会用，raw socket 不会）
用例：
  A. 代理端口 127.0.0.1:7890 是否在监听
  B. stdlib urllib 走系统代理 GET pypi.org
  C. stdlib urllib 强制直连（ProxyHandler({})）GET pypi.org
  D. urllib3（venv pip 的 1.26.12）用 **调试日志** 走一次，看它卡在哪
  E. urllib3 显式指定 proxy=None / 直连
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
BUILD = os.path.join(ROOT, "build")
os.makedirs(BUILD, exist_ok=True)

print("=" * 88)
print("A. 代理端口是否在监听（stdlib，无第三方依赖）", flush=True)
print("=" * 88)
code = r"""
import socket
for host, port in (("127.0.0.1", 7890), ("127.0.0.1", 7891), ("127.0.0.1", 1080)):
    s = socket.socket()
    s.settimeout(2.0)
    try:
        s.connect((host, port))
        print(f"  {host}:{port}  ✅ 可连")
    except Exception as exc:
        print(f"  {host}:{port}  ❌ {type(exc).__name__}")
    finally:
        s.close()
"""
path = os.path.join(BUILD, "diag5_a.py")
with open(path, "w", encoding="utf-8") as handle:
    handle.write(code)
print(subprocess.run([VENV_PY, path], capture_output=True, text=True, encoding="utf-8",
                     errors="replace", timeout=60).stdout)

print("=" * 88)
print("B/C. stdlib urllib：走系统代理 vs 强制直连", flush=True)
print("=" * 88)
code = r"""
import json, time, urllib.request
out = {}
for label, handler in (("走系统代理", None), ("强制直连", urllib.request.ProxyHandler({}))):
    try:
        if handler is None:
            opener = urllib.request.build_opener()
        else:
            opener = urllib.request.build_opener(handler)
        t0 = time.time()
        with opener.open("https://pypi.org/simple/", timeout=10) as resp:
            out[label] = f"OK status={resp.status} {time.time()-t0:.1f}s"
    except Exception as exc:
        out[label] = f"FAIL {type(exc).__name__}: {str(exc)[:120]}"
print("R " + json.dumps(out, ensure_ascii=False))
"""
path = os.path.join(BUILD, "diag5_b.py")
with open(path, "w", encoding="utf-8") as handle:
    handle.write(code)
result = subprocess.run([VENV_PY, path], capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=120)
for line in (result.stdout or "").splitlines():
    if line.startswith("R "):
        payload = json.loads(line[2:])
        for key, value in payload.items():
            print(f"  {key:<10} {value}")
if not (result.stdout or "").strip():
    print("  ❌ 无输出", (result.stderr or "")[:300])

print("=" * 88)
print("D. urllib3 走一次 + 调试日志（看它卡在哪一步）", flush=True)
print("=" * 88)
code = r"""
import logging, sys, time
logging.basicConfig(level=logging.DEBUG, stream=sys.stdout,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
import pip._vendor.urllib3 as u
print("urllib3 =", u.__version__, u.__file__, flush=True)
try:
    pool = u.PoolManager(retries=False, timeout=6.0)
    t0 = time.time()
    resp = pool.request("GET", "https://pypi.org/simple/")
    print(f"RESULT OK status={resp.status} {time.time()-t0:.1f}s", flush=True)
except Exception as exc:
    print(f"RESULT FAIL {type(exc).__name__}: {str(exc)[:200]}", flush=True)
"""
path = os.path.join(BUILD, "diag5_d.py")
with open(path, "w", encoding="utf-8") as handle:
    handle.write(code)
try:
    result = subprocess.run([VENV_PY, path], capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=45)
    lines = (result.stdout or "").splitlines()
    print("\n".join(f"  {line}" for line in lines[:40]))
    if len(lines) > 40:
        print(f"  … 共 {len(lines)} 行")
except subprocess.TimeoutExpired as exc:
    print("  ⏱ 45 秒硬超时")
    partial = (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
    for line in partial.splitlines()[-25:]:
        print(f"  {line}")

print()
print("=" * 88)
print("E. urllib3 显式绕开代理（proxy 相关参数）", flush=True)
print("=" * 88)
code = r"""
import json, ssl, time
import pip._vendor.urllib3 as u
out = {"urllib3": u.__version__}
# 1) 显式传 proxies={} 期望绕过环境代理
try:
    pool = u.ProxyManager  # noqa
except Exception:
    pass
for label, kwargs in (
    ("proxies={}", {"proxies": {}}),
    ("proxy=None", {"proxy": None}),
):
    try:
        t0 = time.time()
        pool = u.PoolManager(retries=False, timeout=6.0, **kwargs)
        resp = pool.request("GET", "https://pypi.org/simple/")
        out[label] = f"OK status={resp.status} {time.time()-t0:.1f}s"
    except Exception as exc:
        out[label] = f"FAIL {type(exc).__name__}: {str(exc)[:120]}"
print("R " + json.dumps(out, ensure_ascii=False))
"""
path = os.path.join(BUILD, "diag5_e.py")
with open(path, "w", encoding="utf-8") as handle:
    handle.write(code)
try:
    result = subprocess.run([VENV_PY, path], capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=60)
    for line in (result.stdout or "").splitlines():
        if line.startswith("R "):
            for key, value in json.loads(line[2:]).items():
                print(f"  {key:<14} {value}")
except subprocess.TimeoutExpired:
    print("  ⏱ 60 秒硬超时（说明绕代理不生效，仍在走代理）")
