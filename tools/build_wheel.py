"""构建 wheel，并把前端构建产物一起打进去。

为什么需要这个脚本：

* 后端只在**找不到** `web/dist` 时才回退到内建提示页，所以一个"只有 Python 代码"的
  wheel 装到别的机器上会让面板变成降级页 —— 而用户装 wheel 的预期是"装完就能看面板"。
* setuptools 不会跨目录收录 `web/dist`，本项目也没有引入 hatch/poetry 之类的构建后端，
  所以这里用最直白的做法：构建前把 `web/dist` 摆到 `src/bili_danmaku/web_dist/`，
  构建后清理掉。这样做的好处是 `pip install .`（不带前端）依然可用、源码树保持干净。

前提：`web/dist` 必须已经构建（`cd web && npm run build`）。没有它时脚本会明确报错，
而不是默默产出一个人人都会踩坑的"裸 wheel"。

用法:
    python tools/build_wheel.py                    # 前端已构建
    python tools/build_wheel.py --skip-frontend     # 明知不需要前端
    python tools/build_wheel.py --out dist --keep   # 保留暂存目录便于排查
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGING = ROOT / "src" / "bili_danmaku" / "web_dist"


def _log(message: str) -> None:
    print(message, flush=True)


def stage_frontend() -> Path:
    dist = ROOT / "web" / "dist"
    if not (dist / "index.html").is_file():
        raise SystemExit(
            "找不到 web/dist/index.html。\n"
            "请先构建前端：cd web && npm install && npm run build\n"
            "（确实不需要面板时，用 --skip-frontend 跳过）"
        )

    if STAGING.exists():
        shutil.rmtree(STAGING)
    shutil.copytree(dist, STAGING)

    size = sum(path.stat().st_size for path in STAGING.rglob("*") if path.is_file())
    files = sum(1 for path in STAGING.rglob("*") if path.is_file())
    _log(f"已暂存前端产物: web/dist → {STAGING.relative_to(ROOT)}（{files} 个文件，{size // 1024} KB）")
    return STAGING


def cleanup(keep: bool) -> None:
    if keep or not STAGING.exists():
        return
    shutil.rmtree(STAGING)
    _log(f"已清理暂存目录 {STAGING.relative_to(ROOT)}")


def build(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    # --no-build-isolation：本机 pip 网络不可用，构建依赖用 wheels/ 里的现成 wheel
    command = [
        sys.executable,
        "-m",
        "pip",
        "wheel",
        ".",
        "--no-deps",
        "--no-build-isolation",
        "-w",
        str(out_dir),
    ]
    _log("$ " + " ".join(command))
    result = subprocess.run(command, cwd=ROOT, check=False)
    if result.returncode != 0:
        raise SystemExit(f"构建失败，退出码 {result.returncode}")


def main() -> int:
    parser = argparse.ArgumentParser(description="构建带前端产物的 wheel")
    parser.add_argument("--out", default="dist", help="wheel 输出目录（默认 dist）")
    parser.add_argument("--skip-frontend", action="store_true", help="不打包前端（产物只能显示降级页）")
    parser.add_argument("--keep", action="store_true", help="构建后保留 src/bili_danmaku/web_dist")
    args = parser.parse_args()

    staged = False
    try:
        if not args.skip_frontend:
            stage_frontend()
            staged = True
        build(ROOT / args.out)
    finally:
        if staged:
            cleanup(args.keep)

    wheels = sorted((ROOT / args.out).glob("bili_danmaku-*.whl"))
    if wheels:
        _log(f"\n产物: {wheels[-1]}")
        _log("校验安装（离线）：")
        _log(
            f"  .venv\\Scripts\\python.exe -m pip install --no-input --no-index "
            f"--find-links wheels {wheels[-1]}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
