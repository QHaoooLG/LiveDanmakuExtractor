"""PyInstaller 可行性实测：能否把本项目的核心链路打成**单文件 exe**。

测的四件事：
  1. PyInstaller 能不能在 venv 的**转发器 stub** 下正常工作；
  2. 单文件 exe 能不能**真的跑起来**（导入 fastapi/uvicorn/websockets + 建 app）；
  3. 体积与冷启动耗时（单文件 exe 每次解压，这是它最大的代价）；
  4. 打成 GUI 程序（`--windowed`）后，托盘 + tkinter 是否可用。

不写进 src/，全部放 build/pyi/ 下做隔离实验。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYI = os.path.join(ROOT, "build", "pyi")
SRC = os.path.join(PYI, "src")
DIST = os.path.join(PYI, "dist")
WORK = os.path.join(PYI, "work")
SPEC = os.path.join(PYI, "spec")

for path in (SRC, DIST, WORK, SPEC):
    os.makedirs(path, exist_ok=True)

# --------------------------------------------------------------------------- #
# 探针入口：导入核心链路 + 建 app + 报告
# --------------------------------------------------------------------------- #
PROBE = '''"""冻结后的自检入口：把启动链路上真正要用的东西都 import 一遍。"""
import json, os, sys, time

t0 = time.time()
report = {"frozen": getattr(sys, "frozen", False), "executable": sys.executable}

try:
    import fastapi, uvicorn, httpx, websockets, pydantic, brotli, click  # noqa: F401
    report["import_web_stack"] = "ok"
except Exception as exc:
    report["import_web_stack"] = f"FAIL {type(exc).__name__}: {exc}"

try:
    import bili_danmaku
    report["import_bili_danmaku"] = getattr(bili_danmaku, "__file__", "?")
    from bili_danmaku.server import PanelService, WebHub, bind_port, create_app, find_dist
    report["import_server"] = "ok"
    report["find_dist"] = str(find_dist())
except Exception as exc:
    report["import_server"] = f"FAIL {type(exc).__name__}: {exc}"

try:
    hub = WebHub()
    report["hub_ok"] = hub is not None
except Exception as exc:
    report["hub_ok"] = f"FAIL {type(exc).__name__}: {exc}"

try:
    from bili_danmaku import runtime
    report["runtime_ok"] = runtime.__name__
except Exception as exc:
    report["runtime_ok"] = f"FAIL {type(exc).__name__}: {exc}"

# GUI 依赖（托盘方案要用）
for mod in ("tkinter", "PIL.Image", "ctypes"):
    try:
        __import__(mod)
        report[f"import_{mod}"] = "ok"
    except Exception as exc:
        report[f"import_{mod}"] = f"FAIL {type(exc).__name__}: {exc}"

report["elapsed_s"] = round(time.time() - t0, 3)
print("PYI_REPORT " + json.dumps(report, ensure_ascii=False))

# 有 --selftest-out 就把报告写文件（--windowed 时 stdout 不可见）
argv = sys.argv[1:]
if "--selftest-out" in argv:
    out = argv[argv.index("--selftest-out") + 1]
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
'''

probe_path = os.path.join(SRC, "pyi_probe.py")
with open(probe_path, "w", encoding="utf-8") as handle:
    handle.write(PROBE)

print("=" * 92)
print("1. PyInstaller 环境自检", flush=True)
print("=" * 92)
version = subprocess.run(
    [sys.executable, "-m", "PyInstaller", "--version"],
    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
)
print(f"  PyInstaller 版本 = {(version.stdout or version.stderr).strip()}")
print(f"  构建用解释器     = {sys.executable}")

# --------------------------------------------------------------------------- #
# 单文件构建
# --------------------------------------------------------------------------- #
print()
print("=" * 92)
print("2. 构建单文件 exe（--onefile，核心链路）", flush=True)
print("=" * 92)

web_dist = os.path.join(ROOT, "web", "dist")
add_data = []
if os.path.isdir(web_dist):
    add_data += ["--add-data", f"{web_dist}{os.pathsep}web_dist"]
    print(f"  前端产物：{web_dist} -> web_dist")
else:
    print("  ⚠️ web/dist 不存在，跳过前端打包")

cmd = [
    sys.executable, "-m", "PyInstaller",
    "--noconfirm", "--clean",
    "--onefile",
    "--name", "pyi_probe",
    "--paths", os.path.join(ROOT, "src"),
    "--distpath", DIST,
    "--workpath", WORK,
    "--specpath", SPEC,
    # 明确排掉重型可选依赖，看能瘦到多少（jieba/matplotlib 只有词云 png 才需要）
    "--exclude-module", "matplotlib",
    "--exclude-module", "numpy",
    "--exclude-module", "wordcloud",
    "--exclude-module", "jieba",
    "--exclude-module", "PIL",
    "--exclude-module", "pytest",
    "--exclude-module", "tkinter",
    *add_data,
    probe_path,
]

t0 = time.time()
result = subprocess.run(
    cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
    timeout=1800, cwd=ROOT,
)
build_s = time.time() - t0
tail = ((result.stdout or "") + (result.stderr or "")).strip().splitlines()
print(f"  构建 rc={result.returncode}  耗时 {build_s:.0f}s")
for line in tail[-8:]:
    print(f"    {line.strip()[:150]}")

exe = os.path.join(DIST, "pyi_probe.exe")
if not os.path.isfile(exe):
    print("\n  ❌ 没有产出 exe，构建失败")
    raise SystemExit(1)

size_mb = os.path.getsize(exe) / (1024 * 1024)
print(f"\n  ✅ 产出 {exe}")
print(f"     体积 {size_mb:.1f} MB")

# --------------------------------------------------------------------------- #
# 运行验证
# --------------------------------------------------------------------------- #
print()
print("=" * 92)
print("3. 运行验证（冷启动耗时 + 依赖是否齐全）", flush=True)
print("=" * 92)

report_file = os.path.join(PYI, "run_report.json")
if os.path.exists(report_file):
    os.remove(report_file)

t0 = time.time()
run = subprocess.run(
    [exe, "--selftest-out", report_file],
    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
)
cold_s = time.time() - t0
print(f"  rc={run.returncode}  冷启动 {cold_s:.2f}s")

payload = None
if os.path.isfile(report_file):
    with open(report_file, encoding="utf-8") as handle:
        payload = json.load(handle)
else:
    for line in (run.stdout or "").splitlines():
        if line.startswith("PYI_REPORT "):
            payload = json.loads(line[len("PYI_REPORT ") :])

if payload:
    for key, value in payload.items():
        mark = ""
        if isinstance(value, str) and value.startswith("FAIL"):
            mark = "  ❌"
        print(f"      {key:<26} {value}{mark}")
else:
    print("  ❌ 拿不到报告")
    print(f"     stdout={(run.stdout or '')[:400]}")
    print(f"     stderr={(run.stderr or '')[:600]}")

print()
print("=" * 92)
print("汇总")
print("=" * 92)
print(f"  单文件 exe 可用 : {'✅' if payload and not any(str(v).startswith('FAIL') for v in payload.values()) else '⚠️ 见上'}")
print(f"  体积            : {size_mb:.1f} MB")
print(f"  冷启动          : {cold_s:.2f}s")
print(f"  构建耗时        : {build_s:.0f}s")
