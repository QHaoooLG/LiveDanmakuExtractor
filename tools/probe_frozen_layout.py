"""探针 10：PyInstaller 冻结后 `__file__` / `_MEIPASS` 的真实布局。

动机：`find_dist()` 用 `Path(__file__).resolve().parent.parent / "web_dist"`。
PyInstaller **onefile** 会把 `bili_danmaku` 放进 `_MEIPASS/bili_danmaku/...`，
但 **onedir** 会把包打进 `_internal/base_library.zip`，此时
`_MEIPASS/bili_danmaku/` **根本不存在** ⇒ `__file__` 会落到 `<exe目录>/bili_danmaku/...`，
于是 `find_dist()` 找不到 `_internal/web_dist` —— 这是必须要知道的坑。

本探针把两种模式的真实目录布局打出来，不猜。
"""

from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYI2 = os.path.join(ROOT, "build", "pyi2")
RUNDIR = os.path.join(PYI2, "rundir2")
os.makedirs(RUNDIR, exist_ok=True)

PROBE = r'''
import json, os, sys

def listing(path, limit=25):
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

rep["MEIPASS_listing"] = listing(getattr(sys, "_MEIPASS", "."))
rep["pkg_dir_listing"] = listing(os.path.dirname(bili_danmaku.__file__))

# web_dist 的所有可能位置，逐个查
meipass = getattr(sys, "_MEIPASS", None)
exe_dir = os.path.dirname(sys.executable)
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

with open(os.path.join(os.environ["PYI_OUT_DIR"],
                       os.environ.get("PYI_LAYOUT_NAME", "layout.json")), "w", encoding="utf-8") as fh:
    json.dump(rep, fh, ensure_ascii=False, indent=2)
print(json.dumps(rep, ensure_ascii=False, indent=2))
'''

probe_path = os.path.join(PYI2, "src", "layout_probe.py")
os.makedirs(os.path.dirname(probe_path), exist_ok=True)
with open(probe_path, "w", encoding="utf-8") as handle:
    handle.write(PROBE)

TARGETS = [
    ("onefile", os.path.join(PYI2, "dist", "pyi_gui.exe")),
    ("onedir", os.path.join(PYI2, "dist_onedir", "pyi_gui_dir", "pyi_gui_dir.exe")),
]

env = dict(os.environ)
env["PYI_OUT_DIR"] = RUNDIR
env["PYTHONIOENCODING"] = "utf-8"

for label, exe in TARGETS:
    print("=" * 92)
    print(f"【{label}】{exe}")
    print("=" * 92)
    if not os.path.isfile(exe):
        # 旧构建里自检脚本叫 pyi_gui_probe；这里直接说明缺失
        print("  ⚠️ 该 exe 不存在（可能被清理），跳过")
        continue
    out = os.path.join(RUNDIR, "layout.json")
    if os.path.exists(out):
        os.remove(out)
    env_run = dict(env)
    env_run["PYI_LAYOUT_NAME"] = f"layout_{label}.json"
    result = subprocess.run(
        [exe], capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300, cwd=RUNDIR, env=env_run,
    )
    out = os.path.join(RUNDIR, f"layout_{label}.json")
    if not os.path.isfile(out):
        # 旧 exe 里的自检脚本不写 layout.json，退回读 stdout
        print(f"  （该 exe 内嵌的是旧探针，stdout 前 600 字）")
        print((result.stdout or "")[:600])
        print(f"  stderr: {(result.stderr or '')[:300]}")
        continue
    import json as _json

    with open(out, encoding="utf-8") as handle:
        rep = _json.load(handle)
    for key in ("_MEIPASS", "cwd", "pkg___file__", "pkg_dir", "server_app___file__"):
        print(f"  {key:<22} {rep.get(key)}")
    print(f"  MEIPASS_listing        {rep.get('MEIPASS_listing')}")
    print(f"  pkg_dir_listing        {rep.get('pkg_dir_listing')}")
    print("  web_dist 候选命中：")
    for key, value in (rep.get("web_dist_candidates") or {}).items():
        print(f"      {key:<28} {value}")
    print(f"  find_dist()            {rep.get('find_dist')}")
    print()
