"""环境诊断：确认 pip 网络不可用时的可行绕过路径。

只读操作，不修改任何环境。
"""

from __future__ import annotations

import json
import re
import sys

import httpx

print("=== 1. 系统 Python 已安装的包 ===")
import importlib.metadata as md

installed = {}
for dist in md.distributions():
    name = (dist.metadata["Name"] or "").strip()
    if name:
        installed[name.lower()] = dist.version
for name in sorted(installed):
    print(f"  {name:28s} {installed[name]}")

print("\n=== 2. 关键依赖是否已就绪 ===")
need = [
    "httpx", "websockets", "brotli", "click", "pydantic", "fastapi",
    "uvicorn", "qrcode", "pytest", "pytest-asyncio", "packaging",
    "anyio", "h11", "httpcore", "certifi", "idna", "sniffio",
    "starlette", "typing-extensions", "annotated-types", "pydantic-core",
    "colorama", "iniconfig", "pluggy",
]
for name in need:
    mark = "OK  " if name in installed else "MISS"
    print(f"  [{mark}] {name:24s} {installed.get(name, '')}")

print("\n=== 3. httpx 能否从 pypi.org 解析并下载 wheel ===")
client = httpx.Client(timeout=20.0, follow_redirects=True)
try:
    html = client.get("https://pypi.org/simple/brotli/").text
    urls = re.findall(r'href="([^"]+\.whl[^"]*)"', html)
    print(f"  simple 索引: {len(urls)} 个 wheel 链接")
    candidates = [u for u in urls if "cp311" in u and "win_amd64" in u]
    print(f"  cp311/win_amd64 候选: {len(candidates)}")
    if candidates:
        url = candidates[-1]
        resp = client.get(url)
        print(
            f"  下载测试: HTTP {resp.status_code}  {len(resp.content)} bytes  "
            f"host={url.split('/')[2]}"
        )
except Exception as exc:  # noqa: BLE001
    print(f"  [FAIL] {type(exc).__name__}: {exc}")

print("\n=== 4. PyPI JSON API 可用性（用于解析依赖）===")
try:
    payload = client.get("https://pypi.org/pypi/fastapi/json").json()
    info = payload["info"]
    print(f"  fastapi 最新版: {info['version']}")
    print(f"  requires_dist: {info['requires_dist']}")
except Exception as exc:  # noqa: BLE001
    print(f"  [FAIL] {type(exc).__name__}: {exc}")

print(f"\npython: {sys.version.split()[0]}  {sys.executable}")
