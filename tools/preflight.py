"""M0 前置预检：依赖可用性 + B 站接口连通性 + 协议握手前提。

只读操作，不发弹幕、不改任何服务端状态。
用法: python tools/preflight.py [房间号]
"""

from __future__ import annotations

import importlib
import json
import sys

ROOM_INPUT = sys.argv[1] if len(sys.argv) > 1 else "804456"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
LIVE_HEADERS = {"User-Agent": UA, "Referer": "https://live.bilibili.com/"}

REQUIRED = ["httpx", "websockets", "brotli", "click", "pydantic", "fastapi", "uvicorn"]
# (import 名, 展示名)：注意 websocket-client 的 import 名是 websocket
OPTIONAL = [("qrcode", "qrcode"), ("websocket", "websocket-client"), ("jieba", "jieba")]


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> int:
    section("1. Python 与依赖")
    print(f"python: {sys.version.split()[0]} ({sys.executable})")
    for name in REQUIRED:
        try:
            mod = importlib.import_module(name)
            print(f"  [OK]      {name} {getattr(mod, '__version__', '?')}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [MISSING] {name}  ({type(exc).__name__})")
    for import_name, label in OPTIONAL:
        try:
            mod = importlib.import_module(import_name)
            print(f"  [OK]      {label} {getattr(mod, '__version__', '?')} (可选)")
        except Exception:  # noqa: BLE001
            print(f"  [absent]  {label} (可选)")

    try:
        import httpx
    except Exception:  # noqa: BLE001
        print("\nhttpx 不可用，无法继续网络检查。")
        return 1

    client = httpx.Client(headers=LIVE_HEADERS, timeout=10.0, follow_redirects=True)

    section(f"2. 房间解析 room_init (输入 {ROOM_INPUT})")
    real_room = None
    anchor_uid = None
    try:
        r = client.get(
            "https://api.live.bilibili.com/room/v1/Room/room_init",
            params={"id": ROOM_INPUT},
        )
        j = r.json()
        print(f"  HTTP {r.status_code}  code={j.get('code')}  msg={j.get('message')}")
        d = j.get("data") or {}
        real_room = d.get("room_id")
        anchor_uid = d.get("uid")
        print(
            f"  真实房间号={real_room}  主播uid={anchor_uid}  "
            f"live_status={d.get('live_status')} (0未开播/1直播中/2轮播)  "
            f"live_time={d.get('live_time')}"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] {type(exc).__name__}: {exc}")
        return 2

    section("3. 设备指纹 finger/spi (buvid3/buvid4)")
    buvid3 = None
    try:
        r = client.get("https://api.bilibili.com/x/frontend/finger/spi")
        j = r.json()
        d = j.get("data") or {}
        buvid3 = d.get("b_3")
        print(f"  HTTP {r.status_code}  code={j.get('code')}")
        print(f"  b_3(buvid3)={ (buvid3 or '')[:12] }...  len={len(buvid3 or '')}")
        print(f"  b_4(buvid4) 存在={bool(d.get('b_4'))}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] {type(exc).__name__}: {exc}")

    section("4. WS 认证信息 getDanmuInfo")
    target = real_room or ROOM_INPUT
    for label, extra_headers, extra_params in (
        ("匿名(无 buvid)", {}, {"id": target, "type": "0"}),
        (
            "匿名(带 buvid3)",
            {"Cookie": f"buvid3={buvid3}"} if buvid3 else {},
            {"id": target, "type": "0"},
        ),
        ("仅 id 参数", {}, {"id": target}),
    ):
        try:
            r = client.get(
                "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo",
                params=extra_params,
                headers=extra_headers,
            )
            j = r.json()
            code = j.get("code")
            d = j.get("data") or {}
            hosts = d.get("host_list") or []
            print(
                f"  [{label}] HTTP {r.status_code} code={code} msg={j.get('message')!r} "
                f"token={'有' if d.get('token') else '无'} 节点数={len(hosts)}"
            )
            if hosts:
                h = hosts[0]
                print(
                    f"      首选节点 wss://{h.get('host')}:{h.get('wss_port')} "
                    f"(tcp {h.get('port')} / ws {h.get('ws_port')})"
                )
                print(f"      token 长度={len(d.get('token') or '')}")
                print(f"      max_delay={d.get('max_delay')} refresh_rate={d.get('refresh_rate')}")
            else:
                print(f"      原始响应: {json.dumps(j, ensure_ascii=False)[:400]}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [{label}] [FAIL] {type(exc).__name__}: {exc}")

    section("5. 兜底接口 Danmu/getConf")
    try:
        r = client.get(
            "https://api.live.bilibili.com/room/v1/Danmu/getConf",
            params={"room_id": target, "platform": "pc", "player": "web"},
        )
        j = r.json()
        d = j.get("data") or {}
        print(
            f"  HTTP {r.status_code} code={j.get('code')} "
            f"token={'有' if d.get('token') else '无'} 节点数={len(d.get('host_server_list') or [])}"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] {type(exc).__name__}: {exc}")

    section("6. 历史弹幕 gethistory (离线夹具来源)")
    try:
        r = client.get(
            "https://api.live.bilibili.com/xlive/web-room/v1/dM/gethistory",
            params={"roomid": target, "room_type": "0"},
        )
        j = r.json()
        d = j.get("data") or {}
        room_msgs = d.get("room") or []
        print(f"  HTTP {r.status_code} code={j.get('code')} 最近弹幕条数={len(room_msgs)}")
        for m in room_msgs[:3]:
            print(
                f"      [{m.get('timeline')}] {m.get('nickname')}: "
                f"{str(m.get('text'))[:40]}"
            )
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] {type(exc).__name__}: {exc}")

    section("7. 登录态接口 nav (check-auth 依据)")
    try:
        r = client.get("https://api.bilibili.com/x/web-interface/nav")
        j = r.json()
        d = j.get("data") or {}
        print(f"  HTTP {r.status_code} code={j.get('code')} isLogin={d.get('isLogin')}")
        print(f"  wbi_img 可用={bool((d.get('wbi_img') or {}).get('img_url'))} (WBI 签名前提)")
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] {type(exc).__name__}: {exc}")

    print("\n=== 预检结束 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
