"""构建 onefile + onedir 两个「布局探针」exe，用于确定冻结后的目录布局。

合并了此前两个脚本的职责：构建 -> 运行 -> 打印布局。
非破坏性：只复制 web/dist 的副本用于打包，绝不动本体。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYI = os.path.join(ROOT, "build", "pyi3")
SRC = os.path.join(PYI, "src")
SPEC = os.path.join(PYI, "spec")
RUNDIR = os.path.join(PYI, "rundir")
for path in (SRC, SPEC, RUNDIR):
    os.makedirs(path, exist_ok=True)

PROBE = r'''
import json, os, sys

def listing(path, limit=30):
    try:
        return sorted(os.listdir(path))[:limit]
    except Exception as exc:
        return f"<无法列举 {type(exc).__name__}>"

rep = {}
rep["frozen"] = bool(getattr(sys, "frozen", False))
rep["_MEIPASS"] = getattr(sys, "_MEIPASS", None)
rep["sys.executable"] = sys.executable
rep["cwd"] = os.getcwd()

import bili_danmaku
rep["pkg___file__"] = bili_danmaku.__file__
rep["pkg_dir"] = os.path.dirname(bili_danmaku.__file__)

from bili_danmaku.server import app as server_app
rep["server_app___file__"] = server_app.__file__

meipass = getattr(sys, "_MEIPASS", None)
exe_dir = os.path.dirname(sys.executable)
rep["MEIPASS_listing"] = listing(meipass or ".")
rep["exe_dir_listing"] = listing(exe_dir)
rep["pkg_dir_listing"] = listing(os.path.dirname(bili_danmaku.__file__))

cands = {
    "MEIPASS/web_dist": os.path.join(meipass, "web_dist") if meipass else None,
    "exe_dir/web_dist": os.path.join(exe_dir, "web_dist"),
    "exe_dir/_internal/web_dist": os.path.join(exe_dir, "_internal", "web_dist"),
    "pkg_dir/web_dist": os.path.join(os.path.dirname(bili_danmaku.__file__), "web_dist"),
}
rep["web_dist_candidates"] = {
    k: (os.path.isfile(os.path.join(v, "index.html")) if v else None)
    for k, v in cands.items()
}

from bili_danmaku.server import find_dist
rep["find_dist"] = str(find_dist())

name = os.environ.get("PYI_LAYOUT_NAME", "layout.json")
with open(os.path.join(os.environ["PYI_OUT_DIR"], name), "w", encoding="utf-8") as fh:
    json.dump(rep, fh, ensure_ascii=False, indent=2)
print(json.dumps(rep, ensure_ascii=False, indent=2))
'''

probe_path = os.path.join(SRC, "layout_probe.py")
with open(probe_path, "w", encoding="utf-8") as handle:
    handle.write(PROBE)

# 非破坏性：复制前端产物副本
web_dist = os.path.join(ROOT, "web", "dist")
staged = os.path.join(PYI, "staged_dist")
if os.path.isdir(staged):
    shutil.rmtree(staged, ignore_errors=True)
shutil.copytree(web_dist, staged)
print(f"已复制前端产物 {sum(1 for _,_,n in os.walk(staged) for _ in n)} 个文件到隔离目录")

COMMON = [
    sys.executable, "-m", "PyInstaller",
    "--noconfirm", "--clean",
    "--paths", os.path.join(ROOT, "src"),
    "--specpath", SPEC,
    "--exclude-module", "matplotlib",
    "--exclude-module", "numpy",
    "--exclude-module", "wordcloud",
    "--exclude-module", "jieba",
    "--exclude-module", "pytest",
    "--add-data", f"{staged}{os.pathsep}web_dist",
    probe_path,
]

built: list[tuple[str, str]] = []
for mode, distpath, workpath, name in (
    ("onefile", os.path.join(PYI, "dist_onefile"), os.path.join(PYI, "work_onefile"), "layout_onefile"),
    ("onedir", os.path.join(PYI, "dist_onedir"), os.path.join(PYI, "work_onedir"), "layout_onedir"),
):
    flag = "--onefile" if mode == "onefile" else "--onedir"
    cmd = [*COMMON, flag, "--name", name, "--distpath", distpath, "--workpath", workpath]
    print(f"\n构建 {mode} …", flush=True)
    t0 = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=1800, cwd=ROOT)
    print(f"  rc={result.returncode}  {time.time()-t0:.0f}s")
    if mode == "onefile":
        exe = os.path.join(distpath, f"{name}.exe")
    else:
        exe = os.path.join(distpath, name, f"{name}.exe")
    if os.path.isfile(exe):
        built.append((mode, exe))
        if mode == "onefile":
            print(f"  体积 {os.path.getsize(exe)/(1024*1024):.1f} MB")
        else:
            total = sum(os.path.getsize(os.path.join(r, f))
                        for r, _, fs in os.walk(os.path.dirname(exe)) for f in fs)
            print(f"  目录总体积 {total/(1024*1024):.1f} MB")
    else:
        print(f"  ❌ 未产出；末尾日志：")
        for line in ((result.stdout or "") + (result.stderr or "")).strip().splitlines()[-6:]:
            print(f"     {line.strip()[:140]}")

print()
print("=" * 92)
print("运行布局探针")
print("=" * 92)
env = dict(os.environ)
env["PYI_OUT_DIR"] = RUNDIR
env["PYTHONIOENCODING"] = "utf-8"

for mode, exe in built:
    env["PYI_LAYOUT_NAME"] = f"layout_{mode}.json"
    out = os.path.join(RUNDIR, f"layout_{mode}.json")
    if os.path.exists(out):
        os.remove(out)
    t0 = time.time()
    subprocess.run([exe], capture_output=True, text=True, encoding="utf-8",
                   errors="replace", timeout=300, cwd=RUNDIR, env=env)
    cold = time.time() - t0
    print(f"\n【{mode}】冷启动 {cold:.2f}s")
    if not os.path.isfile(out):
        print("  ❌ 无报告")
        continue
    with open(out, encoding="utf-8") as handle:
        rep = json.load(handle)
    for key in ("_MEIPASS", "cwd", "pkg___file__", "pkg_dir", "server_app___file__"):
        print(f"  {key:<22} {rep.get(key)}")
    print(f"  MEIPASS_listing        {rep.get('MEIPASS_listing')}")
    print(f"  exe_dir_listing        {rep.get('exe_dir_listing')}")
    print(f"  pkg_dir_listing        {rep.get('pkg_dir_listing')}")
    print("  web_dist 候选命中：")
    for key, value in (rep.get("web_dist_candidates") or {}).items():
        print(f"      {key:<30} {value}")
    print(f"  find_dist()            {rep.get('find_dist')}")

# 清理隔离副本，绝不动 web/dist 本体
if os.path.isdir(staged):
    shutil.rmtree(staged, ignore_errors=True)
print()
print(f"前端本体完好：{os.path.isfile(os.path.join(web_dist, 'index.html'))}")
