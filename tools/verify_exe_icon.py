"""验证 exe 图标**真的换成了新图**（需求 1）。

`--icon` 把 ico 写进 exe 的**资源段**，所以这里直接从 PE 资源里把
`RT_GROUP_ICON` / `RT_ICON` 读出来、重新拼成 ico，再和 `assets/app.ico` 逐像素比对。

为什么不"看构建日志说成功"：日志只能证明 PyInstaller **尝试**写图标，
证明不了写进去的是哪张图（换图时最容易出"改了源码但没重新生成 ico"这类错）。
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PIL import Image  # noqa: E402

EXE = ROOT / "dist" / "exe" / "B站弹幕提取姬.exe"
ICO = ROOT / "assets" / "app.ico"
SOURCE = ROOT / "pics" / "B站弹幕提取姬_团子形象2.png"
OLD_SOURCE = ROOT / "pics" / "b站弹幕提取姬图标_透明背景版.png"

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {label}{('  ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


# --------------------------------------------------------------------------- #
# 从 PE 里抽图标
# --------------------------------------------------------------------------- #

RT_ICON = 3
RT_GROUP_ICON = 14


def _rva_to_offset(sections, rva: int) -> int:
    for virtual_address, virtual_size, raw_pointer, raw_size in sections:
        if virtual_address <= rva < virtual_address + max(virtual_size, raw_size):
            return raw_pointer + (rva - virtual_address)
    raise ValueError(f"RVA {rva:#x} 不在任何节里")


def extract_ico(path: Path) -> bytes:
    """把 PE 里的图标资源重新拼成一个 .ico 字节串。"""
    data = path.read_bytes()
    if data[:2] != b"MZ":
        raise ValueError("不是 PE 文件")
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe_offset:pe_offset + 4] != b"PE\0\0":
        raise ValueError("PE 签名不对")

    coff = pe_offset + 4
    section_count = struct.unpack_from("<H", data, coff + 2)[0]
    optional_size = struct.unpack_from("<H", data, coff + 16)[0]
    optional = coff + 20
    magic = struct.unpack_from("<H", data, optional)[0]
    # PE32+ 与 PE32 的 data directory 偏移不同
    directories = optional + (112 if magic == 0x20B else 96)
    resource_rva = struct.unpack_from("<I", data, directories + 2 * 8)[0]

    section_table = optional + optional_size
    sections = []
    for index in range(section_count):
        base = section_table + index * 40
        virtual_size, virtual_address, raw_size, raw_pointer = struct.unpack_from(
            "<IIII", data, base + 8
        )
        sections.append((virtual_address, virtual_size, raw_pointer, raw_size))

    root = _rva_to_offset(sections, resource_rva)

    def children(offset: int) -> list[tuple[int, int]]:
        """读一个资源目录节点的子项：`[(name_or_id, child_and_flags)]`。

        计数在节点偏移 +12/+14（named, ids），项从 +16 开始，每项 8 字节：
        4 字节 name（或 id）、4 字节 child（最高位 = 1 表示子节点也是目录）。
        """
        named, ids = struct.unpack_from("<HH", data, offset + 12)
        out: list[tuple[int, int]] = []
        for index in range(named + ids):
            name, child = struct.unpack_from("<II", data, offset + 16 + index * 8)
            out.append((name, child))
        return out

    def find_leaf(type_id: int, wanted_id: int | None = None) -> tuple[int, bytes] | None:
        """按 `类型 → (id) → 语言` 走到叶子，返回 `(id, 资源字节)`。"""
        for name, child in children(root):
            if name != type_id:
                continue
            for sub_name, sub_child in children(root + (child & 0x7FFFFFFF)):
                if wanted_id is not None and sub_name != wanted_id:
                    continue
                for _lang, lang_child in children(root + (sub_child & 0x7FFFFFFF)):
                    entry = root + (lang_child & 0x7FFFFFFF)
                    data_rva, size = struct.unpack_from("<II", data, entry)
                    if data_rva == 0:
                        continue
                    offset = _rva_to_offset(sections, data_rva)
                    return int(sub_name), data[offset:offset + size]
        return None

    group_hit = find_leaf(RT_GROUP_ICON)
    if group_hit is None:
        raise ValueError("exe 里没有 RT_GROUP_ICON（图标没写进去）")
    _group_id, group = group_hit

    # 把该 type 下**所有** icon 都读出来（按 id 索引），再按 group 里的 id 取用
    icon_blobs: dict[int, bytes] = {}
    for name, child in children(root):
        if name != RT_ICON:
            continue
        for sub_name, sub_child in children(root + (child & 0x7FFFFFFF)):
            for _lang, lang_child in children(root + (sub_child & 0x7FFFFFFF)):
                entry = root + (lang_child & 0x7FFFFFFF)
                data_rva, size = struct.unpack_from("<II", data, entry)
                if data_rva == 0:
                    continue
                offset = _rva_to_offset(sections, data_rva)
                icon_blobs[int(sub_name)] = data[offset:offset + size]

    # GRPICONDIR: reserved(2) type(2) count(2) + count * GRPICONDIRENTRY(14)
    count = struct.unpack_from("<H", group, 4)[0]
    images = []
    for index in range(count):
        base = 6 + index * 14
        width, height, colors, _res, planes, bits, size, icon_id = struct.unpack_from(
            "<BBBBHHIH", group, base
        )
        blob = icon_blobs.get(icon_id)
        if blob is None:
            continue
        images.append((width, height, colors, planes, bits, size, blob))

    # 组装 .ico
    out = bytearray(struct.pack("<HHH", 0, 1, len(images)))
    offset = 6 + len(images) * 16
    payload = bytearray()
    for width, height, colors, planes, bits, size, blob in images:
        out += struct.pack(
            "<BBBBHHII", width, height, colors, 0, planes, bits, len(blob), offset
        )
        payload += blob
        offset += len(blob)
    return bytes(out + payload)


print("=" * 82)
print("1. 资源文件")
print("=" * 82)
with Image.open(SOURCE) as src:
    print(f"  源图      {SOURCE.name}  {src.size} {src.mode}")
with Image.open(ICO) as ico:
    ico_sizes = sorted(ico.ico.sizes())
print(f"  app.ico   {len(ico_sizes)} 档：{ico_sizes}")
check("app.ico 含 16/32/48/256", {(16, 16), (32, 32), (48, 48), (256, 256)} <= set(ico_sizes))

print()
print("=" * 82)
print("2. 从 exe 的 PE 资源里抽图标")
print("=" * 82)
if not EXE.is_file():
    print(f"  ❌ 找不到 {EXE}（先跑 tools/build_exe.py）")
    raise SystemExit(1)
raw_ico = extract_ico(EXE)
tmp = ROOT / "build" / "_exe_icon.ico"
tmp.parent.mkdir(parents=True, exist_ok=True)
tmp.write_bytes(raw_ico)
print(f"  抽出 {len(raw_ico)} 字节 -> {tmp.name}")

with Image.open(tmp) as exe_icon:
    exe_sizes = sorted(exe_icon.ico.sizes())
print(f"  exe 内嵌图标 {len(exe_sizes)} 档：{exe_sizes}")
check("exe 内嵌图标尺寸与 app.ico 一致", exe_sizes == ico_sizes)

# 逐档逐像素比对（取最大那档）
biggest = exe_sizes[-1]
with Image.open(tmp) as frame:
    frame.size = biggest
    exe_frame = frame.convert("RGBA").copy()
with Image.open(ICO) as frame:
    frame.size = biggest
    ico_frame = frame.convert("RGBA").copy()

pixels = list(zip(exe_frame.getdata(), ico_frame.getdata()))
diff = sum(1 for a, b in pixels if a != b)
total = len(pixels)
print(f"  与 assets/app.ico 的差异像素 = {diff} / {total}")
check("exe 图标 == app.ico（逐像素）", diff == 0, f"差异率 {diff / total:.3%}")

if OLD_SOURCE.is_file():
    with Image.open(OLD_SOURCE) as raw:
        old_frame = raw.convert("RGBA").resize(biggest).copy()
    old_diff = sum(1 for a, b in zip(exe_frame.getdata(), old_frame.getdata()) if a != b)
    print(f"  与**旧**图标({OLD_SOURCE.name}) 差异像素 = {old_diff} / {total}")
    check("确实换成了新图（与旧图不同）", old_diff > 0, f"差异率 {old_diff / total:.2%}")

print()
print("=" * 82)
print("3. 托盘读的是同一份 ico")
print("=" * 82)
from bili_danmaku.gui._win32 import find_icon, load_icon  # noqa: E402

found = find_icon()
print(f"  find_icon() = {found}")
check("托盘能找到 app.ico", bool(found and found.is_file()))
check("托盘用的就是 app.ico", found == ICO)
hicon = load_icon()
check("能加载出 HICON", hicon != 0, f"hicon={hicon}")

print()
print("=" * 82)
if failures:
    print(f"❌ 失败 {len(failures)} 项：{failures}")
    raise SystemExit(1)
print("✅ exe 图标与托盘图标都是新的「团子形象2」")
print("=" * 82)
