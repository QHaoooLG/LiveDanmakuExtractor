"""M0 协议握手实测：验证匿名路径能否真正收到弹幕，并抓取真实样本。

只用标准库 zlib + 已装的 websocket-client，不引入新依赖。
只读：仅连接、认证、心跳、接收，不发送任何弹幕或互动。

用法: python tools/handshake_check.py [房间号] [采集秒数]
"""

from __future__ import annotations

import json
import struct
import sys
import time
import zlib
from collections import Counter
from pathlib import Path

import httpx

sys.stdout.reconfigure(encoding="utf-8")

ROOM_INPUT = sys.argv[1] if len(sys.argv) > 1 else "804456"
DURATION = float(sys.argv[2]) if len(sys.argv) > 2 else 25.0

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
LIVE_HEADERS = {"User-Agent": UA, "Referer": "https://live.bilibili.com/"}

HEADER = struct.Struct(">IHHII")  # 总长, 头长, protover, operation, sequence
HEADER_LEN = 16

OP_HEARTBEAT = 2
OP_HEARTBEAT_REPLY = 3
OP_MESSAGE = 5
OP_AUTH = 7
OP_AUTH_REPLY = 8

PROTO_PLAIN = 0
PROTO_HEARTBEAT = 1
PROTO_ZLIB = 2
PROTO_BROTLI = 3


def pack(operation: int, payload: bytes, protover: int = PROTO_PLAIN) -> bytes:
    return HEADER.pack(HEADER_LEN + len(payload), HEADER_LEN, protover, operation, 1) + payload


def unpack(data: bytes) -> list[tuple[int, int, bytes]]:
    """拆包，返回 [(protover, operation, payload)]，自动递归解压嵌套包。"""
    out: list[tuple[int, int, bytes]] = []
    offset = 0
    total = len(data)
    while offset + HEADER_LEN <= total:
        pkt_len, hdr_len, protover, operation, _seq = HEADER.unpack_from(data, offset)
        if pkt_len < hdr_len or offset + pkt_len > total:
            raise ValueError(f"非法包长 {pkt_len} (剩余 {total - offset})")
        body = data[offset + hdr_len : offset + pkt_len]
        if protover == PROTO_ZLIB:
            out.extend(unpack(zlib.decompress(body)))
        elif protover == PROTO_BROTLI:
            import brotli  # 需额外安装

            out.extend(unpack(brotli.decompress(body)))
        else:
            out.append((protover, operation, body))
        offset += pkt_len
    return out


def main() -> int:
    import websocket  # websocket-client，import 名是 websocket

    client = httpx.Client(headers=LIVE_HEADERS, timeout=10.0, follow_redirects=True)

    j = client.get(
        "https://api.live.bilibili.com/room/v1/Room/room_init", params={"id": ROOM_INPUT}
    ).json()
    real_room = (j.get("data") or {}).get("room_id")
    anchor_uid = (j.get("data") or {}).get("uid")
    live_status = (j.get("data") or {}).get("live_status")
    print(f"[房间] 输入={ROOM_INPUT} 真实={real_room} 主播uid={anchor_uid} live_status={live_status}")

    conf = client.get(
        "https://api.live.bilibili.com/room/v1/Danmu/getConf",
        params={"room_id": real_room, "platform": "pc", "player": "web"},
    ).json()
    cdata = conf.get("data") or {}
    token = cdata.get("token")
    hosts = cdata.get("host_server_list") or []
    print(f"[getConf] code={conf.get('code')} token={'有' if token else '无'} 节点数={len(hosts)}")
    if not hosts:
        print("[FATAL] 无可用节点")
        return 2
    for h in hosts[:3]:
        print(f"   节点 {h.get('host')} wss={h.get('wss_port')} ws={h.get('ws_port')}")

    errors: list[str] = []
    for host_info in hosts[:4]:
        host = host_info.get("host")
        port = host_info.get("wss_port") or 443
        url = f"wss://{host}:{port}/sub"
        print(f"\n===== 尝试连接 {url} =====")
        ws = None
        try:
            ws = websocket.create_connection(
                url,
                timeout=10,
                origin="https://live.bilibili.com",
                header=[
                    f"User-Agent: {UA}",
                    "Referer: https://live.bilibili.com/",
                ],
            )
            print("  [1/4] 连接成功")

            auth = {
                "uid": 0,
                "roomid": real_room,
                "protover": 2,  # 只声明 zlib，避免依赖 brotli
                "platform": "web",
                "type": 2,
                "key": token,
            }
            ws.send_binary(pack(OP_AUTH, json.dumps(auth, separators=(",", ":")).encode()))
            raw = ws.recv()
            for protover, operation, body in unpack(raw):
                if operation == OP_AUTH_REPLY:
                    info = json.loads(body)
                    print(f"  [2/4] 认证回应 op={operation} protover={protover} body={info}")
                    if info.get("code") != 0:
                        raise RuntimeError(f"认证失败 code={info.get('code')}")

            ws.send_binary(pack(OP_HEARTBEAT, b"[object Object]", PROTO_HEARTBEAT))
            raw = ws.recv()
            for protover, operation, body in unpack(raw):
                if operation == OP_HEARTBEAT_REPLY:
                    popularity = struct.unpack(">I", body[:4])[0] if len(body) >= 4 else None
                    print(f"  [3/4] 心跳回应 op={operation} 人气值={popularity}")
            break
        except Exception as exc:  # noqa: BLE001
            print(f"  [FAIL] {type(exc).__name__}: {exc}")
            errors.append(f"{host}: {type(exc).__name__}: {exc}")
            if ws is not None:
                try:
                    ws.close()
                except Exception:  # noqa: BLE001
                    pass
    else:
        print("\n[FATAL] 所有节点均失败：")
        for e in errors:
            print(f"  - {e}")
        return 3

    print(f"\n[4/4] 开始采集 {DURATION:.0f}s ...")
    ws.settimeout(3)
    deadline = time.time() + DURATION
    next_hb = time.time() + 25
    counts: Counter[str] = Counter()
    samples: list[dict] = []
    total_msgs = 0

    while time.time() < deadline:
        if time.time() >= next_hb:
            ws.send_binary(pack(OP_HEARTBEAT, b"[object Object]", PROTO_HEARTBEAT))
            next_hb = time.time() + 25
        try:
            raw = ws.recv()
        except websocket.WebSocketTimeoutException:
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"  接收中断: {type(exc).__name__}: {exc}")
            break
        if not raw:
            continue
        try:
            packets = unpack(raw)
        except Exception as exc:  # noqa: BLE001
            print(f"  解包失败: {type(exc).__name__}: {exc}")
            continue
        for protover, operation, body in packets:
            if operation != OP_MESSAGE:
                continue
            try:
                msg = json.loads(body)
            except Exception:  # noqa: BLE001
                continue
            cmd = str(msg.get("cmd", ""))
            base = cmd.split(":")[0]
            counts[base] += 1
            total_msgs += 1
            if base == "DANMU_MSG" and len(samples) < 3:
                samples.append(msg)
                print(f"\n  --- DANMU_MSG 样本 #{len(samples)} ---")
                print(f"  {json.dumps(msg, ensure_ascii=False)[:900]}")

    print(f"\n===== 采集结果 =====")
    print(f"总消息数={total_msgs}")
    for cmd, n in counts.most_common():
        print(f"  {cmd:36s} {n}")

    if samples:
        out = Path("out_capture")
        out.mkdir(parents=True, exist_ok=True)
        path = out / "danmu_msg_samples.json"
        path.write_text(
            json.dumps(samples, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n样本已保存: {path}")

    try:
        ws.close()
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
