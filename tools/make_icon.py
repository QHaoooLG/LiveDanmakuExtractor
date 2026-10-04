"""从 `pics/` 的源图生成多尺寸 `.ico`，供 exe 图标与托盘图标共用。

为什么要生成而不是直接引用 PNG：
  * Windows 的 exe 图标需要 **`.ico`**（`--icon` 只吃 ico，不吃 png）；
  * 托盘图标也需要 ico（`LoadImageW` + `LR_LOADFROMFILE` 读 ico 最省事）；
  * 源图是 1254×1254 RGBA，直接当 16×16 用会糊，
    必须**预先降采样**成多档尺寸，让 Windows 按 DPI 挑合适的那档。

产物固定为 `assets/app.ico`（已 gitignore 之外，入库；体积很小）。

用法:
    python tools/make_icon.py                 # 用默认源图
    python tools/make_icon.py --check         # 只校验产物是否存在且尺寸齐全
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: 源图（需求方指定）。换图标时改这一行即可 —— exe 图标与托盘图标都用它。
DEFAULT_SOURCE = ROOT / "pics" / "B站弹幕提取姬_团子形象2.png"
DEFAULT_OUTPUT = ROOT / "assets" / "app.ico"

#: 覆盖 Windows 常见的图标尺寸（含 16/32/48/256 这几档必备的）
ICON_SIZES = [(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)]


def build(source: Path, output: Path) -> int:
    """把 `source` 转成多尺寸 ico 写到 `output`。返回文件字节数。"""
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的明确提示
        raise SystemExit(
            "缺少 Pillow，无法生成 ico。安装：\n"
            "  .\\.venv\\Scripts\\python.exe -m pip install pillow"
        ) from exc

    if not source.is_file():
        raise SystemExit(f"找不到源图：{source}")

    with Image.open(source) as raw:
        image = raw.convert("RGBA")
        source_size = image.size
        # 已经是正方形时直接缩；非正方先补成正方，避免 ico 里被拉变形
        if source_size[0] != source_size[1]:
            side = max(source_size)
            canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
            canvas.paste(image, ((side - source_size[0]) // 2, (side - source_size[1]) // 2))
            image = canvas
        output.parent.mkdir(parents=True, exist_ok=True)
        image.save(output, format="ICO", sizes=ICON_SIZES)

    size = output.stat().st_size
    print(f"源图        {source}  {source_size[0]}×{source_size[1]}")
    print(f"已生成      {output}  {size} 字节")
    print(f"尺寸档位    {', '.join(f'{w}x{h}' for w, h in ICON_SIZES)}")
    return size


def check(output: Path) -> int:
    """校验产物存在、可读、且包含期望的尺寸档位。"""
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("缺少 Pillow") from exc

    if not output.is_file():
        print(f"❌ 产物不存在：{output}")
        return 1

    with Image.open(output) as icon:
        available = sorted(icon.ico.sizes()) if hasattr(icon, "ico") else {icon.size}
    expected = set(ICON_SIZES)
    missing = sorted(expected - set(available))
    print(f"产物        {output}  {output.stat().st_size} 字节")
    print(f"包含尺寸    {sorted(available)}")
    if missing:
        print(f"❌ 缺少尺寸  {missing}")
        return 1
    print("✅ 尺寸齐全")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 exe/托盘共用图标")
    parser.add_argument("--source", default=str(DEFAULT_SOURCE), help="源 PNG")
    parser.add_argument("--out", default=str(DEFAULT_OUTPUT), help="输出 ico")
    parser.add_argument("--check", action="store_true", help="只校验产物，不重新生成")
    args = parser.parse_args()

    output = Path(args.out)
    if args.check:
        return check(output)
    build(Path(args.source), output)
    return check(output)


if __name__ == "__main__":
    sys.exit(main())
