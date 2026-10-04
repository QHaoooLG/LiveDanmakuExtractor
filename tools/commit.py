"""用 **UTF-8 消息文件**提交（绕开 PowerShell 的引号与编码坑）。

存在的理由（本项目反复踩过）：

* PowerShell 5 **没有** `<<<`（here-string 重定向），`git commit -m @'...'@`
  里含中文 + 引号/反引号时经常被截断或变成乱码；
* 这个仓库的文件名与提交信息大量是中文，`git commit -m "<中文>"` 在
  GBK 控制台下会被转成 `?`。

所以统一走"写文件 → `git commit -F`"。本脚本就是那条路径的固化版本。

用法::

    # 消息从 stdin 读（推荐：配合文件重定向，避免任何 shell 引号处理）
    python tools/commit.py -m build/msg.txt            # 直接用现成的消息文件
    python tools/commit.py --amend -m build/msg.txt

    # 只做检查，不提交
    python tools/commit.py --dry-run
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: 消息暂存文件（放在 gitignore 覆盖的 build/ 下，不会污染版本库）
DEFAULT_MSG = ROOT / "build" / "commit_msg.txt"


def run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="用 UTF-8 消息文件提交")
    parser.add_argument("-m", "--message-file", default=None, help="消息文件（默认 build/commit_msg.txt）")
    parser.add_argument("--amend", action="store_true", help="追加到上一次提交")
    parser.add_argument("--dry-run", action="store_true", help="只显示将要提交的内容")
    parser.add_argument("--no-add", action="store_true", help="不执行 git add -A")
    args = parser.parse_args()

    if not args.no_add:
        run(["add", "-A"])

    staged = run(["diff", "--cached", "--name-status"]).stdout.strip()
    if not staged:
        print("没有待提交的改动。", file=sys.stderr)
        return 1
    print("将要提交：")
    for line in staged.splitlines():
        print(f"  {line}")

    if args.dry_run:
        print("\n（--dry-run：未提交）")
        return 0

    message_file = Path(args.message_file) if args.message_file else DEFAULT_MSG
    if not message_file.is_file():
        print(
            f"找不到提交消息文件：{message_file}\n"
            "先把消息写成 UTF-8 文件再提交（这正是本脚本要解决的问题）。",
            file=sys.stderr,
        )
        return 1

    text = message_file.read_text(encoding="utf-8").strip()
    if not text:
        print(f"提交消息为空：{message_file}", file=sys.stderr)
        return 1

    # 校验编码：提交信息必须是合法 UTF-8（中文仓库里这一步很值）
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:  # pragma: no cover - 极端情况
        print(f"提交消息不是合法 UTF-8：{exc}", file=sys.stderr)
        return 1

    cmd = ["commit", "-F", str(message_file)]
    if args.amend:
        cmd.append("--amend")
    result = run(cmd, check=False)
    if result.returncode != 0:
        print(f"提交失败（rc={result.returncode}）：", file=sys.stderr)
        print((result.stdout or "") + (result.stderr or ""), file=sys.stderr)
        return result.returncode

    print()
    print(run(["log", "--oneline", "-1"]).stdout.strip())
    print()
    print(run(["show", "--stat", "--oneline", "HEAD"]).stdout.strip().splitlines()[-1])
    return 0


if __name__ == "__main__":
    sys.exit(main())
