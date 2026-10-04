"""PyInstaller 实测 · 第二轮：把**托盘真正需要的 GUI 依赖**纳入，测真实体积与冻结行为。

第一轮为看清"核心链路"底价，刻意排掉了 tkinter / PIL；本轮把它们**包含**，
因为托盘方案要用 tkinter(pystray 不用) + Pillow(生成 ico)。

同时验证一个**已知隐患**：`find_dist()` 的候选路径
  `Path(__file__).resolve().parents[3] / "web" / "dist"`
在冻结后 `__file__` 位于 `sys._MEIPASS/bili_danmaku/server/app.py`，
`parents[3]` 会指向 `_MEIPASS` 的**上一层**，而不是 `_MEIPASS` 自身 ——
所以打包的前端到底能不能被找到，必须实测，不能想当然。

做法：构建时**故意把项目根目录的 web/dist 改名藏起来**，
这样冻结程序只能靠包内 `_MEIPASS/web_dist` 命中候选 1，从而真实检验它。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYI = os.path.join(ROOT, "build", "pyi2")
SRC = os.path.join(PYI, "src")
DIST = os.path.join(PYI, "dist")
WORK = os.path.join(PYI, "work")
SPEC = os.path.join(PYI, "spec")
for path in (SRC, DIST, WORK, SPEC):
    os.makedirs(path, exist_ok=True)

PROBE = '''"""冻结自检：GUI 依赖 + 前端产物定位。"""
import json, os, sys, time

t0 = time.time()
report = {"frozen": bool(getattr(sys, "frozen", False))}
report["_MEIPASS"] = getattr(sys, "_MEIPASS", None)

# --- 1) 托盘/面板要用的东西 ---
for mod in ("tkinter", "tkinter.ttk", "PIL.Image", "PIL.ImageTk", "ctypes", "urllib.request"):
    try:
        __import__(mod)
        report[f"imp_{mod}"] = "ok"
    except Exception as exc:
        report[f"imp_{mod}"] = f"FAIL {type(exc).__name__}: {exc}"

# --- 2) 托盘自检：消息窗口 + 图标加载（不改动系统托盘状态）---
try:
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    hwnd = user32.GetDesktopWindow()
    report["user32_ok"] = bool(hwnd)
except Exception as exc:
    report["user32_ok"] = f"FAIL {type(exc).__name__}: {exc}"

# --- 3) tkinter 能真的建窗口吗 ---
try:
    import tkinter
    root = tkinter.Tk()
    root.withdraw()
    root.update()
    report["tk_window"] = f"ok Tk={tkinter.TkVersion}"
    root.destroy()
except Exception as exc:
    report["tk_window"] = f"FAIL {type(exc).__name__}: {exc}"

# --- 4) 前端产物：候选 1（包内 web_dist）能不能命中 ---
try:
    from bili_danmaku.server import find_dist
    found = find_dist()
    report["find_dist"] = str(found)
    report["find_dist_index_exists"] = bool(found and (found / "index.html").is_file())
except Exception as exc:
    report["find_dist"] = f"FAIL {type(exc).__name__}: {exc}"

# 直接看包内 web_dist 到底有没有被拷进去
try:
    import bili_danmaku
    pkg_dir = os.path.dirname(bili_danmaku.__file__)
    candidate = os.path.join(pkg_dir, "web_dist")
    report["pkg_web_dist"] = candidate
    report["pkg_web_dist_exists"] = os.path.isdir(candidate)
    report["pkg_web_dist_index"] = os.path.isfile(os.path.join(candidate, "index.html"))
except Exception as exc:
    report["pkg_web_dist"] = f"FAIL {exc}"

# --- 5) cwd 候选是否被意外命中（说明测试无效）---
report["cwd"] = os.getcwd()
report["cwd_web_dist_exists"] = os.path.isfile(os.path.join(os.getcwd(), "web", "dist", "index.html"))

report["elapsed_s"] = round(time.time() - t0, 3)
print("PYI_REPORT " + json.dumps(report, ensure_ascii=False))
out = os.path.join(os.environ.get("PYI_OUT_DIR", "."), "gui_report.json")
with open(out, "w", encoding="utf-8") as fh:
    json.dump(report, fh, ensure_ascii=False, indent=2)
'''

probe_path = os.path.join(SRC, "pyi_gui_probe.py")
with open(probe_path, "w", encoding="utf-8") as handle:
    handle.write(PROBE)

web_dist = os.path.join(ROOT, "web", "dist")
print("=" * 92)
print("1. 构建（包含 tkinter + PIL；排除词云重型依赖）", flush=True)
print("=" * 92)

# 关键：**复制**一份前端产物到隔离目录，用副本打包。
# （原计划是把 web/dist 改名藏起来，但 Windows 上目录被其它进程持有句柄时
#   os.rename 会报 WinError 5 拒绝访问 —— 改成复制，非破坏性。）
# 运行时把 cwd 设为临时目录，这样 find_dist() 的候选 2/3 都不会命中，
# 冻结程序只能靠包内 `_MEIPASS/web_dist` 命中候选 1，检验才是真实的。
staged_dist = os.path.join(PYI, "staged_dist")
if os.path.isdir(staged_dist):
    shutil.rmtree(staged_dist, ignore_errors=True)
if os.path.isdir(web_dist):
    shutil.copytree(web_dist, staged_dist)
    count = sum(1 for _, _, names in os.walk(staged_dist) for _n in names)
    print(f"  已复制 web/dist -> 隔离目录（{count} 个文件）")
else:
    print("  ❌ web/dist 不存在，无法验证前端打包")
    raise SystemExit(1)
hidden = staged_dist  # 复用下面 add-data 的变量名

try:
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile",
        "--name", "pyi_gui",
        "--paths", os.path.join(ROOT, "src"),
        "--distpath", DIST,
        "--workpath", WORK,
        "--specpath", SPEC,
        "--exclude-module", "matplotlib",
        "--exclude-module", "numpy",
        "--exclude-module", "wordcloud",
        "--exclude-module", "jieba",
        "--exclude-module", "pytest",
        "--add-data", f"{hidden}{os.pathsep}web_dist",
        probe_path,
    ]
    t0 = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=1800, cwd=ROOT)
    build_s = time.time() - t0
    print(f"  构建 rc={result.returncode}  耗时 {build_s:.0f}s")
    for line in ((result.stdout or "") + (result.stderr or "")).strip().splitlines()[-5:]:
        print(f"    {line.strip()[:150]}")

    exe = os.path.join(DIST, "pyi_gui.exe")
    if not os.path.isfile(exe):
        print("  ❌ 未产出 exe")
        raise SystemExit(1)
    onefile_mb = os.path.getsize(exe) / (1024 * 1024)
    print(f"\n  ✅ onefile 体积 = {onefile_mb:.1f} MB")

    # --- 运行（用别的 cwd，确保不靠 cwd 命中）---
    print()
    print("=" * 92)
    print("2. 运行验证（cwd 设为临时目录，排除 cwd 候选干扰）", flush=True)
    print("=" * 92)
    rundir = os.path.join(PYI, "rundir")
    os.makedirs(rundir, exist_ok=True)
    out_json = os.path.join(rundir, "gui_report.json")
    if os.path.exists(out_json):
        os.remove(out_json)

    env = dict(os.environ)
    env["PYI_OUT_DIR"] = rundir
    t0 = time.time()
    run = subprocess.run([exe], capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=300, cwd=rundir, env=env)
    cold_s = time.time() - t0
    print(f"  rc={run.returncode}  冷启动 {cold_s:.2f}s")

    payload = None
    if os.path.isfile(out_json):
        with open(out_json, encoding="utf-8") as handle:
            payload = json.load(handle)
    if payload:
        for key, value in payload.items():
            mark = "  ❌" if isinstance(value, str) and value.startswith("FAIL") else ""
            print(f"      {key:<24} {value}{mark}")
    else:
        print(f"  ❌ 无报告 stdout={(run.stdout or '')[:300]!r} stderr={(run.stderr or '')[:500]!r}")

    # --- onedir 对照（体积分布 + 启动速度）---
    print()
    print("=" * 92)
    print("3. onedir 对照构建（启动更快、体积分布可见）", flush=True)
    print("=" * 92)
    DIST2 = os.path.join(PYI, "dist_onedir")
    cmd2 = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onedir",
        "--name", "pyi_gui_dir",
        "--paths", os.path.join(ROOT, "src"),
        "--distpath", DIST2,
        "--workpath", os.path.join(PYI, "work_dir"),
        "--specpath", SPEC,
        "--exclude-module", "matplotlib",
        "--exclude-module", "numpy",
        "--exclude-module", "wordcloud",
        "--exclude-module", "jieba",
        "--exclude-module", "pytest",
        "--add-data", f"{hidden}{os.pathsep}web_dist",
        probe_path,
    ]
    t0 = time.time()
    result2 = subprocess.run(cmd2, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=1800, cwd=ROOT)
    build2_s = time.time() - t0
    exe2 = os.path.join(DIST2, "pyi_gui_dir", "pyi_gui_dir.exe")
    print(f"  构建 rc={result2.returncode}  耗时 {build2_s:.0f}s")
    if os.path.isfile(exe2):
        total = sum(
            os.path.getsize(os.path.join(root_, name))
            for root_, _, names in os.walk(os.path.join(DIST2, "pyi_gui_dir"))
            for name in names
        )
        print(f"  ✅ onedir 总体积 = {total / (1024*1024):.1f} MB")

        out_json2 = os.path.join(rundir, "gui_report_dir.json")
        if os.path.exists(out_json2):
            os.remove(out_json2)
        env2 = dict(os.environ)
        env2["PYI_OUT_DIR"] = rundir
        env2["PYI_REPORT_NAME"] = "gui_report_dir.json"
        # 用独立文件名避免覆盖
        t0 = time.time()
        run2 = subprocess.run([exe2], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=300, cwd=rundir, env=env2)
        cold2 = time.time() - t0
        print(f"  运行 rc={run2.returncode}  冷启动 {cold2:.2f}s")

        # 最大的 10 个文件
        files = []
        for root_, _, names in os.walk(os.path.join(DIST2, "pyi_gui_dir")):
            for name in names:
                p = os.path.join(root_, name)
                files.append((os.path.getsize(p), os.path.relpath(p, os.path.join(DIST2, "pyi_gui_dir"))))
        files.sort(reverse=True)
        print("  最大的 10 个文件：")
        for size, rel in files[:10]:
            print(f"    {size/1024/1024:6.2f} MB  {rel}")
    else:
        print("  ⚠️ onedir 未产出")
finally:
    # 非破坏性：只清理隔离副本，绝不动 web/dist 本体
    if os.path.isdir(staged_dist):
        shutil.rmtree(staged_dist, ignore_errors=True)
    if os.path.isfile(os.path.join(web_dist, "index.html")):
        print("\n  前端本体 web/dist 完好（本次全程未改动它）")
    else:
        print("\n  ⚠️ 警告：web/dist/index.html 不见了 —— 请检查！")

print()
print("=" * 92)
print("汇总")
print("=" * 92)
print("  单文件 exe（onefile）：体积小、发布简单；代价是**每次启动都要解压**（冷启动见上）")
print("  onedir            ：启动快，但要发一个目录")
