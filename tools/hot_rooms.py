"""挑一个"热"房间：列出当前在线人数最高的直播间。

用途：压测与多房间验收需要一个热门房间号，而公开接口在无凭据时一律
返回 `-352` 风控（与 `getDanmuInfo` 是同一个风控），所以这里复用
`.secrets/cookies.json` 里的登录态，再用 WBI 签名请求。

只做只读查询，不连接弹幕服务器。

    python tools/hot_rooms.py --top 10
    python tools/hot_rooms.py --areas 1,2,3 --top 20 --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bili_danmaku.api import API_LIVE, BASE_HEADERS, BiliApi  # noqa: E402
from bili_danmaku.config import Config  # noqa: E402
from bili_danmaku.credentials import load_credentials  # noqa: E402

#: 平台分区：1 娱乐 / 2 网游 / 3 手游 / 4 单机 / 5 电台 / 6 赛事
DEFAULT_AREAS = [1, 2, 3, 4, 5, 6]


async def fetch_area(api: BiliApi, parent_area_id: int, page: int = 1) -> list[dict[str, Any]]:
    result = await api._get(  # noqa: SLF001 - 刻意复用限流 + WBI 签名能力
        f"{API_LIVE}/xlive/web-interface/v1/second/getList",
        {
            "platform": "web",
            "parent_area_id": parent_area_id,
            "area_id": 0,
            "sort_type": "online",
            "page": page,
        },
        use_wbi=True,
        retry=2,
    )
    if not result.ok:
        raise RuntimeError(f"分区 {parent_area_id} 查询失败 code={result.code} {result.message}")
    return list(result.data.get("list") or [])


async def collect(areas: list[int], top: int) -> list[dict[str, Any]]:
    config = Config.load(path="definitely-not-a-real-config.toml")
    credentials = load_credentials()
    if not credentials.logged_in:
        raise RuntimeError("需要登录态：先执行 bili-danmaku login（匿名访问会返回 -352）")

    rows: list[dict[str, Any]] = []
    async with BiliApi(config, credentials) as api:
        for area in areas:
            try:
                items = await fetch_area(api, area)
            except Exception as exc:  # noqa: BLE001 - 单个分区失败不影响其它分区
                print(f"  分区 {area} 失败: {exc}", file=sys.stderr)
                continue
            for item in items:
                rows.append(
                    {
                        "room_id": int(item.get("roomid") or 0),
                        "title": item.get("title"),
                        "uname": item.get("uname"),
                        "online": int(item.get("online") or 0),
                        "area": item.get("area_name"),
                        "parent_area": item.get("parent_name"),
                    }
                )

    rows.sort(key=lambda row: row["online"], reverse=True)
    deduped: list[dict[str, Any]] = []
    seen: set[int] = set()
    for row in rows:
        if row["room_id"] in seen:
            continue
        seen.add(row["room_id"])
        deduped.append(row)
    return deduped[:top]


def main() -> int:
    parser = argparse.ArgumentParser(description="列出当前人气最高的直播间")
    parser.add_argument("--areas", default=",".join(str(a) for a in DEFAULT_AREAS), help="分区 ID，逗号分隔")
    parser.add_argument("--top", type=int, default=10, help="返回条数")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    areas = [int(item) for item in args.areas.split(",") if item.strip()]
    try:
        rows = asyncio.run(collect(areas, args.top))
    except Exception as exc:  # noqa: BLE001
        print(f"失败: {exc}", file=sys.stderr)
        return 3

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    if not rows:
        print("没有查到在播房间")
        return 0

    print(f"{'房间号':>12}  {'在线':>8}  分区        主播 / 标题")
    for row in rows:
        print(
            f"{row['room_id']:>12}  {row['online']:>8}  "
            f"{(row['parent_area'] or '')[:6]:<6}  {row['uname']} / {(row['title'] or '')[:40]}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
