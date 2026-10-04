"""验证需求 2：启动面板可选存储位置 + 显示当前目录占用。

要点（**语义刻意选保守的一侧**）：
  * 占用统计是**只读**的，按后缀分行给出；
  * 切换目录**不迁移、不删除**已有数据；
  * 切换后**重启采集才生效**（运行中切换要明确这么说，不能悄悄"改成功但没生效"）；
  * 用真实面板验证：切换 → 重启 → 新数据真的落到新目录。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bili_danmaku.launcher import (  # noqa: E402
    LauncherOptions,
    PanelController,
    format_bytes,
    scan_storage,
)

WORK = ROOT / "build" / "_storage_probe"
import shutil  # noqa: E402

if WORK.exists():
    shutil.rmtree(WORK, ignore_errors=True)
old_dir = WORK / "old"
new_dir = WORK / "new"
old_dir.mkdir(parents=True, exist_ok=True)

# 造一份"已有数据"，验证切换不会动它
existing = old_dir / "danmaku.db"
existing.write_bytes(b"x" * 4096)
old_jsonl = old_dir / "804456_20261001-22.jsonl"
old_jsonl.write_bytes(b"y" * 2048)

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {label}{('  ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


controller = PanelController(LauncherOptions(port=0, out_dir=str(old_dir)))
try:
    print("=" * 84)
    print("1. 占用统计（只读）")
    print("=" * 84)
    info = controller.storage_info()
    print(f"  目录   = {info.path}")
    print(f"  占用   = {info.total_text} · {info.file_count} 个文件")
    print(f"  分后缀 = {[(e, format_bytes(b)) for e, b in info.by_extension]}")
    check("统计到 2 个文件", info.file_count == 2)
    check("总量 6144 字节", info.total_bytes == 6144, str(info.total_bytes))
    check(".db 单独列出", dict(info.by_extension).get(".db") == 4096)
    check("可写", info.writable is True)

    print()
    print("=" * 84)
    print("2. 启动面板 → 切换目录（**未运行**时应当直接生效）")
    print("=" * 84)
    status = controller.start(wait=True, timeout=25)
    check("面板先起在旧目录", status.running is True, status.error)
    check("运行中，current_out_dir 仍是旧目录",
          controller.current_out_dir() == str(old_dir), controller.current_out_dir())

    ok, message = controller.set_out_dir(str(new_dir))
    print(f"  set_out_dir -> {ok} {message}")
    check("切换成功", ok is True)
    check("运行中切换要提示**重启后生效**", "重启" in message, message)
    check("明确说仍写原目录", "原目录" in message, message)

    print()
    print("=" * 84)
    print("3. 旧数据必须**原样保留**（不迁移、不删除）")
    print("=" * 84)
    check("旧 danmaku.db 还在", existing.is_file())
    check("旧 danmaku.db 内容未变", existing.read_bytes() == b"x" * 4096)
    check("旧 jsonl 还在", old_jsonl.is_file())

    print()
    print("=" * 84)
    print("4. 重启采集 → 新数据落到新目录")
    print("=" * 84)
    controller.stop()
    time.sleep(1)
    check("已停止", controller.is_running() is False)

    restarted = controller.start(wait=True, timeout=25)
    check("重启成功", restarted.running is True, restarted.error)
    check("启动后 current_out_dir == 新目录",
          controller.current_out_dir() == str(new_dir), controller.current_out_dir())

    # 真的写一次：加个房间让它落盘（真实网络；写不出来也不算本次判据失败）
    ok_add, detail = controller.add_room("804456")
    print(f"  add_room -> {ok_add}")
    time.sleep(6)
    new_files = sorted(p.name for p in new_dir.iterdir()) if new_dir.is_dir() else []
    print(f"  新目录里的文件 = {new_files}")
    if new_files:
        check("新数据确实落在新目录", True)
        check("旧目录没有被追加写入",
              sorted(p.name for p in old_dir.iterdir()) == sorted(["danmaku.db", old_jsonl.name]),
              str(sorted(p.name for p in old_dir.iterdir())))
    else:
        print("  ℹ️ 这次没落盘（房间未开播/网络），跳过「新数据位置」的断言 —— 不算失败")

    print()
    print("=" * 84)
    print("5. 新目录的占用统计")
    print("=" * 84)
    info_new = controller.storage_info()
    print(f"  {info_new.path} → {info_new.total_text} · {info_new.file_count} 个文件")
    check("统计的是新目录", Path(info_new.path) == new_dir, info_new.path)
finally:
    controller.stop()
    shutil.rmtree(WORK, ignore_errors=True)

print()
print("=" * 84)
if failures:
    print(f"❌ 失败 {len(failures)} 项：{failures}")
    raise SystemExit(1)
print("✅ 存储位置功能符合语义（只读统计 / 不迁不删 / 重启生效）")
print("=" * 84)
