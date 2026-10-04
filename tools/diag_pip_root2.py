"""诊断 4：每个测试跑在**独立子进程**里，各带硬超时，避免一个挂死拖垮全部。

三组，唯一变量是 urllib3 的来源：
  1. venv pip 22.3.1 自带的 urllib3 1.26.12
  2. 全局 pip 26.0.1 自带的 urllib3 1.26.20（插到 sys.path 最前）
  3. 直接 import 全局 site-packages 里的 urllib3（同名顶层包）
每组只做一件事：GET https://pypi.org/simple/ 。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
BASE_PY = r"E:\Python\python.exe"
BUILD = os.path.join(ROOT, "build")
os.makedirs(BUILD, exist_ok=True)

CHILD = r"""
import json, os, ssl, sys, time

mode = sys.argv[1]
info = {"mode": mode, "openssl": ssl.OPENSSL_VERSION}

BASE_SITE = r"E:\Python\Lib\site-packages"

if mode == "venv_vendored":
    import pip
    info["pip"] = pip.__version__
    import pip._vendor.urllib3 as u
elif mode == "global_vendored":
    sys.path.insert(0, BASE_SITE)
    import pip  # 从全局 site-packages 里拿（因为路径插到最前）
    info["pip"] = getattr(pip, "__version__", "?")
    import pip._vendor.urllib3 as u
elif mode == "global_toplevel":
    sys.path.insert(0, BASE_SITE)
    import urllib3 as u
else:
    raise SystemExit("unknown mode")

info["urllib3"] = u.__version__
info["urllib3_file"] = u.__file__

for label, cap in (("tls_default", None), ("tls_1_2", ssl.TLSVersion.TLSv1_2)):
    ctx = ssl.create_default_context()
    if cap is not None:
        ctx.maximum_version = cap
    try:
        pool = u.PoolManager(cert_reqs="CERT_REQUIRED", ssl_context=ctx, retries=False)
        t0 = time.time()
        resp = pool.request("GET", "https://pypi.org/simple/", timeout=8.0)
        info[label] = f"OK status={resp.status} {time.time()-t0:.1f}s"
    except Exception as exc:
        info[label] = f"FAIL {type(exc).__name__}: {str(exc)[:130]}"

with open(sys.argv[2], "w", encoding="utf-8") as fh:
    json.dump(info, fh, ensure_ascii=False)
print(json.dumps(info, ensure_ascii=False))
"""

child = os.path.join(BUILD, "diag4_child.py")
with open(child, "w", encoding="utf-8") as handle:
    handle.write(CHILD)

CASES = [
    ("1. venv pip 自带 urllib3", "venv_vendored"),
    ("2. 全局 pip 自带 urllib3", "global_vendored"),
    ("3. 全局 site-packages 顶层 urllib3", "global_toplevel"),
]

print("=" * 88)
print("每个用例独立子进程 + 40 秒硬超时", flush=True)
print("=" * 88)

summary: list[tuple[str, dict | str]] = []
for label, mode in CASES:
    outfile = os.path.join(BUILD, f"diag4_{mode}.json")
    if os.path.exists(outfile):
        os.remove(outfile)
    try:
        result = subprocess.run(
            [VENV_PY, child, mode, outfile],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=40,
        )
        payload = None
        if os.path.isfile(outfile):
            with open(outfile, encoding="utf-8") as handle:
                payload = json.load(handle)
        if payload is None:
            payload = f"无结果；stdout={(result.stdout or '')[:200]!r} stderr={(result.stderr or '')[:200]!r}"
    except subprocess.TimeoutExpired:
        payload = "⏱ 40 秒硬超时（连接挂死）"
    summary.append((label, payload))
    print(f"\n【{label}】")
    if isinstance(payload, dict):
        for key in ("pip", "urllib3", "urllib3_file", "openssl", "tls_default", "tls_1_2"):
            if key in payload:
                print(f"    {key:<16} {payload[key]}")
    else:
        print(f"    {payload}")

print()
print("=" * 88)
print("判据")
print("=" * 88)
def verdict(payload: object, key: str = "tls_default") -> str:
    if not isinstance(payload, dict):
        return "TIMEOUT/NO-RESULT"
    value = str(payload.get(key, "?"))
    if value.startswith("OK"):
        return "OK"
    if value.startswith("FAIL"):
        return value.split(":", 1)[0].replace("FAIL ", "")
    return "?"

for label, payload in summary:
    print(f"  {label:<38} {verdict(payload)}")

print()
print("  期望：用例 1 失败、用例 2/3 成功 ⇒ 根因 = pip 自带 urllib3 太老")
