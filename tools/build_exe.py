"""把项目打成单文件 exe（托盘 + 面板 + 内置前端产物）。

要点（都是实测踩出来的，改动前请先读）：

1. **入口用 `bili_danmaku.cli:main`**（console script）而不是 `__main__.py` ——
   这样 exe 支持全部子命令（`verify` / `web` / `tray` …），与 `.venv` 里的
   `bili-danmaku.exe` 行为一致。
2. **前端必须放到 `web_dist`**（`--add-data "<web/dist>;web_dist"`）。
   冻结后 `bili_danmaku.__file__` 是**合成路径**（模块在 `base_library.zip` 里），
   磁盘上没有 `<包目录>/`，所以 `find_dist()` 只能靠 `_MEIPASS/web_dist` 命中
   （见 `find_dist()` 的冻结分支说明）。
3. **词云重型依赖显式排除**（matplotlib/numpy/wordcloud/jieba）：只有 CLI
   `wordcloud --format png` 需要它们，托盘/面板都不需要；不排除体积会翻几倍。
   **openpyxl 不排除** —— 统计数据导出的多 sheet xlsx 靠它（见
   `export_stats.build_stats_xlsx`），排掉的话 exe 里就只剩 CSV 了。
4. **打包后必须跑一次 `verify`**：确认前端被真的打进去、且**不是降级页**。
   这一步是 `--onefile` 唯一的"能不能用"判据，不要只看退出码。

用法:
    python tools/build_exe.py                 # 构建 + 自动 verify
    python tools/build_exe.py --skip-verify   # 只构建
    python tools/build_exe.py --onedir        # 构建目录版（启动更快）
    python tools/build_exe.py --keep-work     # 保留中间产物便于排查
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
WEB_DIST = ROOT / "web" / "dist"
STAGING = ROOT / "build" / "exe_staging" / "web_dist"
ICON = ROOT / "assets" / "app.ico"
ASSETS = ROOT / "assets"
WORK = ROOT / "build" / "exe_work"
SPEC = ROOT / "build" / "exe_spec"

APP_NAME = "B站弹幕提取姬"

#: 只有词云 PNG 输出才需要的重型依赖 —— 不要打进面板 exe
EXCLUDES = ("matplotlib", "numpy", "wordcloud", "jieba", "pytest", "tkinter.test")

#: PyInstaller 的入口脚本。
#:
#: **不能直接把 `src/bili_danmaku/cli.py` 当入口** —— PyInstaller 把它当"顶层脚本"
#: 执行，于是 `cli.py` 里的 `from . import __version__` 会因为"没有父包"而报
#: `ImportError: attempted relative import with no known parent package`（实测踩到）。
#: 所以这里生成一个**绝对导入**的引导脚本，再把它作为入口。
ENTRY_TEMPLATE = '''"""PyInstaller 入口（由 tools/build_exe.py 生成，不要手改）。

用绝对导入调用真正的命令行入口，避免相对导入在"顶层脚本"语境下失效。

**关于 `--windowed` 与标准流**：GUI 子系统打包后 `sys.stdout`/`sys.stderr` 是 `None`，
任何 `print()`/`click.echo` 都会 `AttributeError` 崩掉。这里把 None 换成
**`io.StringIO` 黑洞**（而不是日志文件）：

* 黑洞**不会崩**，也不会污染日志 —— 项目自己的 `setup_logging()` 已经用
  `encoding="utf-8"` 写 `.runtime/web.log`，再重定向一次只会把 uvicorn 的
  INFO 行混进日志（实测还把中文写成 GBK 乱码）；
* 真正的可观测性靠日志文件与 `verify --json-out`。

（曾经把标准流重定向到日志文件，结果 uvicorn 的 handler 已绑定旧的 sys.stderr，
重定向反而造成"日志被插入 + 中文乱码"，已改回黑洞。）
"""
import io
import multiprocessing
import sys


def _ensure_std_streams() -> None:
    """windowed 模式下 stdout/stderr 为 None —— 换成 StringIO 黑洞。"""
    if sys.stdout is None:
        sys.stdout = io.StringIO()
    if sys.stderr is None:
        sys.stderr = io.StringIO()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    _ensure_std_streams()
    from bili_danmaku.cli import main

    sys.exit(main())
'''


def log(message: str) -> None:
    print(message, flush=True)


def stage_frontend() -> Path:
    """把 `web/dist` 复制到隔离暂存目录（**不动本体**）。

    早先的版本试过把 `web/dist` 改名再还原，但 Windows 上目录被其它进程持有句柄时
    `os.rename` 会报 `WinError 5 拒绝访问`；复制是安全的做法。
    """
    if not (WEB_DIST / "index.html").is_file():
        raise SystemExit(
            f"找不到前端产物 {WEB_DIST / 'index.html'}。\n"
            "请先构建前端：cd web && npm run build"
        )
    if STAGING.exists():
        shutil.rmtree(STAGING)
    shutil.copytree(WEB_DIST, STAGING)
    files = sum(1 for _ in STAGING.rglob("*") if _.is_file())
    log(f"已暂存前端产物：{WEB_DIST} → {STAGING}（{files} 个文件）")
    return STAGING


def ensure_icon() -> Path:
    """确保 `assets/app.ico` 存在（缺失就现场生成）。"""
    if ICON.is_file():
        return ICON
    log("assets/app.ico 不存在，现场生成…")
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "make_icon.py")],
        cwd=ROOT, check=False, capture_output=True, text=True, encoding="utf-8",
    )
    if not ICON.is_file():
        log(f"⚠️ 图标生成失败，将不带图标构建：{result.stdout} {result.stderr}")
    return ICON


def ensure_entry() -> Path:
    """生成 PyInstaller 的入口脚本（绝对导入），返回其路径。

    见 `ENTRY_TEMPLATE` 的说明：直接把 `cli.py` 当入口会因为相对导入失败。
    """
    staging = ROOT / "build" / "exe_staging"
    staging.mkdir(parents=True, exist_ok=True)
    entry = staging / "_exe_entry.py"
    entry.write_text(ENTRY_TEMPLATE, encoding="utf-8")
    return entry


def build(*, onedir: bool, console: bool = False) -> Path:
    staging = stage_frontend()
    icon = ensure_icon()
    entry = ensure_entry()

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onedir" if onedir else "--onefile",
        # 默认 **--windowed**：用户双击不再弹黑框。
        # 代价是 stdout/stderr 为 None —— 入口脚本已把它们重定向到日志文件，
        # 且 `verify` 用 `--json-out` 落文件取结果（见 verify()）。
        "--console" if console else "--windowed",
        "--name", APP_NAME,
        "--paths", str(SRC),
        "--distpath", str(ROOT / "dist" / "exe"),
        "--workpath", str(WORK / ("onedir" if onedir else "onefile")),
        "--specpath", str(SPEC),
        "--add-data", f"{staging}{os.pathsep}web_dist",
        # 托盘/窗口图标需要**运行时**能找到 app.ico（`--icon` 只管 exe 资源，
        # 不给文件）。放 `assets/app.ico` 与 `_win32.find_icon()` 的候选一致。
        "--add-data", f"{ASSETS / 'app.ico'}{os.pathsep}assets",
        # web 端只需要这些"隐藏导入"能被自动发现，这里不额外列 ——
        # uvicorn 的协议实现按字符串动态加载，显式带上更稳。
        "--hidden-import", "uvicorn.logging",
        "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.protocols.http.auto",
        "--hidden-import", "uvicorn.protocols.websockets.auto",
        "--hidden-import", "uvicorn.lifespan.on",
        *[f"--exclude-module={name}" for name in EXCLUDES],
        str(entry),
    ]
    if icon.is_file():
        cmd += ["--icon", str(icon)]

    log("$ " + " ".join(cmd[:8]) + " …")
    t0 = time.time()
    result = subprocess.run(cmd, cwd=ROOT, check=False, capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    elapsed = time.time() - t0
    log(f"PyInstaller rc={result.returncode}  耗时 {elapsed:.0f}s")
    if result.returncode != 0:
        tail = ((result.stdout or "") + (result.stderr or "")).strip().splitlines()[-15:]
        for line in tail:
            log(f"  {line.strip()[:160]}")
        raise SystemExit("构建失败")

    out_dir = ROOT / "dist" / "exe"
    exe = out_dir / (APP_NAME + ("" if onedir else ".exe"))
    if onedir:
        exe = out_dir / APP_NAME / f"{APP_NAME}.exe"
    if not exe.is_file():
        raise SystemExit(f"没找到产物 {exe}")
    return exe


def report(exe: Path, *, onedir: bool) -> None:
    if onedir:
        total = sum(f.stat().st_size for f in exe.parent.rglob("*") if f.is_file())
        log(f"  产物（目录） {exe.parent}")
        log(f"  目录总体积   {total / 1024 / 1024:.1f} MB")
    else:
        log(f"  产物         {exe}")
        log(f"  体积         {exe.stat().st_size / 1024 / 1024:.1f} MB")


def verify(exe: Path) -> bool:
    """跑冻结自检：这是"exe 到底能不能用"的唯一判据。

    **必须用 `--json-out` 落文件**，不能只读 stdout：`--windowed` 打包后
    stdout 是 `None`（入口脚本会把它重定向到日志文件，所以 stdout 里什么都拿不到）。
    """
    log("")
    log("=" * 78)
    log("冻结自检：exe verify（必须看到「真前端 是」）")
    log("=" * 78)

    out_file = ROOT / "build" / "exe_verify.json"
    out_file.unlink(missing_ok=True)

    t0 = time.time()
    result = subprocess.run(
        [str(exe), "verify", "--json-out", str(out_file)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300,
    )
    cold = time.time() - t0

    payload = None
    if out_file.is_file():
        try:
            payload = json.loads(out_file.read_text(encoding="utf-8"))
        except ValueError as exc:
            log(f"  ⚠️ 结果文件不是合法 JSON：{exc}")

    # 兜底：控制台版（--console）也能从 stdout 拿
    if payload is None:
        text = result.stdout or ""
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                payload = json.loads(text[start : end + 1])
            except ValueError:
                payload = None

    log(f"  冷启动+自检耗时 {cold:.2f}s  rc={result.returncode}")
    if payload is None:
        log("  ❌ 拿不到结果；stdout/stderr：")
        log(f"     stdout={(result.stdout or '')[:600]}")
        log(f"     stderr={(result.stderr or '')[:600]}")
        log("     （windowed 版正常现象就是 stdout 为空；请看 exe_verify.json）")
        return False

    for key in ("frozen", "meipass", "find_dist", "index_exists", "started",
                "http_ok", "served_real_frontend", "url", "detail"):
        if key in payload:
            log(f"    {key:<22} {payload[key]}")

    ok = bool(payload.get("served_real_frontend"))
    log("")
    if ok:
        log("  ✅ exe 能定位并**真的提供构建后的前端**（不是降级页）")
    else:
        log("  ❌ exe 没有提供真前端 —— find_dist() 没命中 _MEIPASS/web_dist？")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="构建单文件 exe")
    parser.add_argument("--onedir", action="store_true", help="构建目录版（启动更快）")
    parser.add_argument("--skip-verify", action="store_true", help="跳过冻结自检")
    parser.add_argument("--keep-work", action="store_true", help="保留中间产物")
    parser.add_argument(
        "--console", action="store_true",
        help="构建控制台版（会弹黑框；调试时才用）",
    )
    args = parser.parse_args()

    try:
        exe = build(onedir=args.onedir, console=args.console)
        report(exe, onedir=args.onedir)
        ok = True if args.skip_verify else verify(exe)
    finally:
        if not args.keep_work:
            shutil.rmtree(STAGING.parent, ignore_errors=True)

    log("")
    log("=" * 78)
    log("说明")
    log("=" * 78)
    kind = "控制台版（会弹黑框）" if args.console else "窗口版（--windowed，双击不弹黑框）"
    log(f"  * 本次构建：{kind}")
    if not args.console:
        log("    windowed 下 stdout/stderr 为 None：入口脚本会把它们重定向到")
        log("    `.runtime/web.log`；`verify` 的结果也落在 exe_verify.json。")
    log(f"  * 运行方式：{exe.name}                  # 双击 = 启动托盘 + 启动面板")
    log(f"              {exe.name} tray --rooms 804456")
    log(f"              {exe.name} verify --json-out build/v.json")
    log(f"              {exe.name} web --port 8791")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
