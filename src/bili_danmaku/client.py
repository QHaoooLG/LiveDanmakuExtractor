"""WebSocket 连接：认证、心跳、收包。

M0 阶段提供单次会话（probe 用）；M1 会在此基础上补状态机与自动重连。

实测要点（见 TECH_DESIGN §1）：
    * 认证包发出后服务端回 op=8 `{"code":0}`。
    * 心跳负载就是字面量 `[object Object]`，回应 op=3 前 4 字节为人气值。
    * protover=2/3 解压后仍是带头部的包，必须递归拆包。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

import websockets

from .config import Config
from .credentials import Credentials
from .models import ConnState, RoomInfo, TokenBundle
from .protocol import (
    HEARTBEAT_PAYLOAD,
    Operation,
    Packet,
    ProtoVer,
    ProtocolError,
    pack,
    parse_popularity,
    resolve_protover,
    unpack,
)

log = logging.getLogger("bili_danmaku.client")

WS_ORIGIN = "https://live.bilibili.com"
WS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://live.bilibili.com/",
}


class AuthFailed(Exception):
    """认证被服务端拒绝。"""

    def __init__(self, code: int) -> None:
        super().__init__(f"认证失败 code={code}")
        self.code = code


class ConnectionStale(Exception):
    """心跳长时间无回应，连接已假死。"""


class ConnectionStats:
    def __init__(self) -> None:
        self.packets = 0
        self.messages = 0
        self.unknown_protover = 0
        self.bad_json = 0
        self.protocol_errors = 0
        self.last_popularity: int | None = None


async def _connect(uri: str, *, open_timeout: float):
    """兼容 websockets 12/13(extra_headers) 与 14+(additional_headers)。"""
    kwargs: dict[str, Any] = {
        "origin": WS_ORIGIN,
        "open_timeout": open_timeout,
        "max_size": None,
        "ping_interval": None,  # 用 B 站自己的心跳，避免协议层 ping 干扰
    }
    try:
        return await websockets.connect(uri, additional_headers=WS_HEADERS, **kwargs)
    except TypeError:  # pragma: no cover - 旧版 websockets
        return await websockets.connect(uri, extra_headers=WS_HEADERS, **kwargs)


class DanmakuConnection:
    """单次 WebSocket 会话。"""

    def __init__(
        self,
        bundle: TokenBundle,
        room: RoomInfo,
        credentials: Credentials,
        config: Config,
        *,
        host_index: int = 0,
    ) -> None:
        self.bundle = bundle
        self.room = room
        self.credentials = credentials
        self.config = config
        self.host_index = host_index
        self.state = ConnState.IDLE
        self.stats = ConnectionStats()
        self.protover = resolve_protover(config.client.protover)
        self.last_heartbeat_reply_at: float = 0.0
        self.auth_reply: dict[str, Any] = {}
        #: 置 True 时保留原始 WS 帧，供 probe --dump-raw 落夹具
        self.capture_raw: bool = False
        self.raw_frames: list[bytes] = []
        self._ws: Any = None

    # ------------------------------------------------------------------ #
    @property
    def uri(self) -> str:
        host = self.bundle.hosts[self.host_index % len(self.bundle.hosts)]
        return host.url()

    async def open(self) -> None:
        self.state = ConnState.CONNECTING
        self._ws = await _connect(self.uri, open_timeout=10.0)

    async def close(self) -> None:
        self.state = ConnState.STOPPED
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None

    async def __aenter__(self) -> "DanmakuConnection":
        await self.open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ------------------------------------------------------------------ #
    async def authenticate(self) -> dict[str, Any]:
        """发送认证包并返回服务端回应（实测 `{"code": 0}`）。"""
        if self._ws is None:
            raise RuntimeError("连接尚未建立")
        self.state = ConnState.AUTH_SENDING

        auth: dict[str, Any] = {
            "uid": self.credentials.dede_user_id,
            "roomid": self.room.room_id,
            "protover": self.protover,
            "platform": "web",
            "type": 2,
            "key": self.bundle.token,
        }
        buvid = self.credentials.buvid3
        if buvid:
            auth["buvid"] = buvid

        await self._ws.send(
            pack(Operation.AUTH, json.dumps(auth, separators=(",", ":")).encode("utf-8"))
        )

        raw = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
        if isinstance(raw, str):
            raw = raw.encode("utf-8")

        for packet in unpack(raw):
            if packet.operation == Operation.AUTH_REPLY:
                payload = json.loads(packet.payload or b"{}")
                if int(payload.get("code") or 0) != 0:
                    raise AuthFailed(int(payload["code"]))
                self.state = ConnState.AUTHED
                self.auth_reply = payload
                # 从这里开始计时：此后若长时间收不到心跳回应即判定假死
                self.last_heartbeat_reply_at = asyncio.get_running_loop().time()
                return payload
        raise AuthFailed(-1)

    async def send_heartbeat(self) -> None:
        if self._ws is None:
            raise RuntimeError("连接尚未建立")
        await self._ws.send(pack(Operation.HEARTBEAT, HEARTBEAT_PAYLOAD, ProtoVer.HEARTBEAT))

    async def recv_packets(self) -> list[Packet]:
        """收一批包。超时抛 asyncio.TimeoutError（由调用方决定是否视为假死）。"""
        if self._ws is None:
            raise RuntimeError("连接尚未建立")
        raw = await asyncio.wait_for(self._ws.recv(), timeout=self.config.client.recv_timeout)
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        self.stats.packets += 1
        if self.capture_raw and len(self.raw_frames) < 50:
            self.raw_frames.append(raw)
        try:
            return unpack(raw)
        except ProtocolError as exc:
            self.stats.protocol_errors += 1
            log.warning("拆包失败: %s", exc)
            return []

    async def messages(
        self, should_stop: Callable[[], bool] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """持续产出 op=5 通知里的 JSON 消息。

        **读超时在这里被吞掉**：低热度房间消息稀疏，`recv_timeout` 超时是常态。
        若让 TimeoutError 逃出生成器，迭代器会被终结，调用方下一次取值拿到
        StopAsyncIteration，从而误判连接结束并触发无谓重连（实测会形成重连风暴）。

        `should_stop` 由调用方注入（通常是「已请求停止」或「已到截止时间」）。
        之所以必须放在生成器**内部**：生成器只在收到消息时才把控制权交回调用方，
        若把截止判断只写在外层循环，无消息时外层永远等不到检查机会（实测死锁）。
        """
        loop = asyncio.get_running_loop()
        while True:
            if should_stop is not None and should_stop():
                return
            try:
                packets = await self.recv_packets()
            except asyncio.TimeoutError:
                self._raise_if_stale(loop)
                continue

            for packet in packets:
                if packet.operation == Operation.HEARTBEAT_REPLY:
                    popularity = parse_popularity(packet.payload)
                    if popularity is not None:
                        self.stats.last_popularity = popularity
                    self.last_heartbeat_reply_at = loop.time()
                    continue
                if packet.operation != Operation.MESSAGE:
                    continue
                self.state = ConnState.STREAMING
                if packet.protover not in (
                    ProtoVer.PLAIN,
                    ProtoVer.ZLIB,
                    ProtoVer.BROTLI,
                ):
                    self.stats.unknown_protover += 1
                    continue
                try:
                    payload = json.loads(packet.payload)
                except ValueError:
                    self.stats.bad_json += 1
                    continue
                self.stats.messages += 1
                yield payload

            self._raise_if_stale(loop)

    def _raise_if_stale(self, loop: asyncio.AbstractEventLoop) -> None:
        if self.last_heartbeat_reply_at <= 0:
            return
        idle = loop.time() - self.last_heartbeat_reply_at
        if idle > self.config.client.heartbeat_timeout:
            raise ConnectionStale(f"心跳已 {idle:.0f} 秒无回应")


async def probe(
    *,
    room: RoomInfo,
    bundle: TokenBundle,
    credentials: Credentials,
    config: Config,
    seconds: float = 30.0,
    max_messages: int = 10,
    on_event: Any = None,
    capture_raw: bool = False,
) -> tuple[DanmakuConnection, list[dict[str, Any]]]:
    """单次会话探测：连接 → 认证 → 心跳 → 采样。

    返回 (connection, 收到的消息列表)。调用方负责关闭 connection。
    认证回应可从 `connection.auth_reply` 取。
    """
    connection = DanmakuConnection(bundle, room, credentials, config)
    connection.capture_raw = capture_raw
    await connection.open()

    auth_reply = await connection.authenticate()
    log.info("认证回应: %s", auth_reply)

    await connection.send_heartbeat()

    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    collected: list[dict[str, Any]] = []

    async def heartbeat_loop() -> None:
        while True:
            await asyncio.sleep(config.client.heartbeat_interval)
            try:
                await connection.send_heartbeat()
            except Exception:  # noqa: BLE001 - 由连接层/超时机制负责重连
                return

    heartbeat = asyncio.create_task(heartbeat_loop(), name="probe-heartbeat")
    stream = connection.messages(should_stop=lambda: loop.time() >= deadline)
    try:
        while len(collected) < max_messages:
            try:
                message = await stream.__anext__()
            except (ConnectionStale, StopAsyncIteration):
                break
            collected.append(message)
            if on_event is not None:
                on_event(message)
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        await stream.aclose()

    return connection, collected
