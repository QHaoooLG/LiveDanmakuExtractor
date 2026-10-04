"""诊断 9：把"哪种禁用代理的写法真正生效"逐个拆开。

诊断 8 结果里有个反直觉之处：
  * B1 默认（走系统代理）        -> 失败 9.2s
  * B2 `--proxy ""`              -> 失败 9.1s   ← 以为能禁用，其实没有
  * B3 清华镜像 + `--proxy ""`    -> 失败 1.2s（另有原因）
  * B4 `NO_PROXY=*` 环境变量      -> ✅ 成功 4.4s

本脚本逐条隔离变量，并顺便验证"升级 venv 的 pip"是否可行。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
BASE_PY = r"E:\Python\python.exe"
BUILD = os.path.join(ROOT, "build")
DEST = os.path.join(BUILD, "pipfix2")

# 每次都用全新空目录，避免缓存掩盖结果
if os.path.isdir(DEST):
    shutil.rmtree(DEST, ignore_errors=True)
os.makedirs(DEST, exist_ok=True)

PKG = "six"  # 极小、纯 python、几乎不可能有兼容问题

CASES = [
    ("1 默认（无任何处理）", {}, []),
    ("2 NO_PROXY=*", {"NO_PROXY": "*", "no_proxy": "*"}, []),
    ("3 NO_PROXY 只列 pypi.org", {"NO_PROXY": "pypi.org,files.pythonhosted.org", "no_proxy": "pypi.org,files.pythonhosted.org"}, []),
    ("4 HTTPS_PROXY 置空", {"HTTPS_PROXY": "", "https_proxy": ""}, []),
    ("5 HTTP_PROXY/HTTPS_PROXY 置空", {"HTTP_PROXY": "", "http_proxy": "", "HTTPS_PROXY": "", "https_proxy": ""}, []),
    ("6 --proxy \"\"（命令行）", {}, ["--proxy", ""]),
    ("7 NO_PROXY=* + --proxy \"\"", {"NO_PROXY": "*", "no_proxy": "*"}, ["--proxy", ""]),
]

print("=" * 92)
print(f"统一用 venv 的 pip（22.3.1）下载 {PKG}，每次全新目录，90s 硬超时", flush=True)
print("=" * 92)

rows = []
for label, env_extra, args in CASES:
    dest = os.path.join(DEST, label.split()[0])
    os.makedirs(dest, exist_ok=True)
    env = dict(os.environ)
    # 先清掉可能残留的代理变量，保证用例之间互不污染
    for key in ("NO_PROXY", "no_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        env.pop(key, None)
    env.update(env_extra)
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [VENV_PY, "-m", "pip", "download", PKG, "--no-deps", "-d", dest, *args]
    t0 = time.time()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=90, env=env)
        elapsed = time.time() - t0
        text = ((result.stdout or "") + (result.stderr or "")).strip()
        rc = result.returncode
        detail = ""
        for line in text.splitlines():
            low = line.lower()
            if "saved" in low:
                detail = line.strip()[:90]
                break
            if "ssl" in low or "could not" in low:
                detail = line.strip()[:90]
        print(f"\n【{label}】rc={rc}  {elapsed:.1f}s")
        if detail:
            print(f"    {detail}")
        rows.append((label, rc, elapsed))
    except subprocess.TimeoutExpired:
        print(f"\n【{label}】⏱ 超时")
        rows.append((label, "TIMEOUT", time.time() - t0))

print()
print("=" * 92)
print("汇总")
print("=" * 92)
for label, rc, elapsed in rows:
    mark = "✅" if rc == 0 else ("⏱" if rc == "TIMEOUT" else "❌")
    print(f"  {mark} {label:<30} rc={str(rc):<8} {elapsed:5.1f}s")

# --------------------------------------------------------------------------- #
# 升级 venv 的 pip 是否可行（这是长期正解）
# --------------------------------------------------------------------------- #
print()
print("=" * 92)
print("升级 venv 的 pip：用全局 pip 26 对 venv 操作（--python）", flush=True)
print("=" * 92)
env = dict(os.environ)
env.pop("HTTP_PROXY", None)
env.pop("http_proxy", None)
env.pop("HTTPS_PROXY", None)
env.pop("https_proxy", None)
env["NO_PROXY"] = "*"
env["no_proxy"] = "*"
env["PYTHONIOENCODING"] = "utf-8"

# 先只做 --dry-run 探测（不改环境）
probe = subprocess.run(
    [BASE_PY, "-m", "pip", "--version"], capture_output=True, text=True, encoding="utf-8",
    errors="replace", timeout=60, env=env
)
print(f"  全局 pip: {(probe.stdout or '').strip()[:110]}")

result = subprocess.run(
    [BASE_PY, "-m", "pip", "install", "--dry-run", "--upgrade", "pip", "--python", VENV_PY],
    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, env=env
)
text = ((result.stdout or "") + (result.stderr or "")).strip()
print(f"  dry-run rc={result.returncode}")
for line in text.splitlines()[-8:]:
    print(f"    {line.strip()[:150]}")

print()
print("  提示：真正的修复动作（由你决定是否执行）：")
print(f"    $env:NO_PROXY='*'; {BASE_PY} -m pip install --upgrade pip --python {VENV_PY}")
