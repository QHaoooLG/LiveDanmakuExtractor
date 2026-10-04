"""脚本化的假弹幕服务端，用于连接状态机的集成测试。

按真实协议行为实现：收 op=7 认证包 → 回 op=8；收 op=2 心跳 → 回 op=3；
之后按脚本推送 op=5 通知（默认用 zlib 压缩，顺带覆盖递归拆包路径）。
"""

from __future__ import annotations

import asyncio
import json
import zlib
from collections.abc import Sequence
from typing import Any

import websockets

from bili_danmaku.protocol import (
    HEARTBEAT_PAYLOAD,
    Operation,
    ProtoVer,
    pack,
    parse_popularity,
    unpack,
)


class FakeDanmakuServer:
    """本地假服务端。

    参数：
        notifications:      认证后依次推送的消息（dict 会被 json 序列化）
        raw_frames:         认证后先原样推送的字节帧，用于构造正常编码
                            造不出来的边界（非法 JSON 负载、未知 protover、截断包）。
                            用途是测客户端的健壮性：这些帧不应该打死读循环。
        auth_code:          认证回应里的 code，非 0 表示认证失败
        auth_fail_times:    前 N 次连接都返回 auth_code，之后转成功（测认证重试）
        silent_heartbeat:   True 则不回心跳（测假死判定）
        drop_after:         推送 N 条通知后主动断开（测自动重连）
        popularity:         心跳回应里的人气值
        compress:           通知是否用 zlib 压缩
        linger:             推送完脚本后**保持连接**的秒数。
                            默认为 0，即推送完立刻关闭（测重连用）。
                            需要"推完再慢慢断言"的用例（如客户端拆包）应设一个正值，
                            否则读端会在断言前先收到 `ConnectionClosedOK`。
    """

    def __init__(
        self,
        *,
        notifications: Sequence[dict[str, Any]] = (),
        raw_frames: Sequence[bytes] = (),
        auth_code: int = 0,
        auth_fail_times: int = 0,
        silent_heartbeat: bool = False,
        drop_after: int | None = None,
        popularity: int = 1234,
        compress: bool = True,
        notify_delay: float = 0.05,
        linger: float = 0.0,
    ) -> None:
        self.notifications = list(notifications)
        self.raw_frames = list(raw_frames)
        self.auth_code = auth_code
        self.auth_fail_times = auth_fail_times
        self.silent_heartbeat = silent_heartbeat
        self.drop_after = drop_after
        self.popularity = popularity
        self.compress = compress
        self.notify_delay = notify_delay
        self.linger = linger
        self.pushed = 0

        self.auth_count = 0
        self.connections = 0
        self.heartbeats = 0
        self.auth_payloads: list[dict[str, Any]] = []
        self._server: Any = None
        self._port = 0

    # ------------------------------------------------------------------ #
    async def __aenter__(self) -> "FakeDanmakuServer":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def start(self) -> None:
        self._server = await websockets.serve(self._handler, "127.0.0.1", 0)
        self._port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @property
    def port(self) -> int:
        return self._port

    @property
    def ws_port(self) -> int:
        """供构造 HostInfo 使用（只有明文端口 → 客户端会用 ws://）。"""
        return self._port

    # ------------------------------------------------------------------ #
    async def _handler(self, websocket: Any) -> None:
        self.connections += 1
        connection_index = self.connections

        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=5.0)
            auth = self._parse_auth(raw)
            if auth is not None:
                self.auth_payloads.append(auth)

            self.auth_count += 1
            fail = self.auth_fail_times > 0 and connection_index <= self.auth_fail_times
            code = self.auth_code if fail else 0

            await websocket.send(self._auth_reply(code))
            if code != 0:
                await websocket.close()
                return

            sender = asyncio.create_task(self._push_notifications(websocket))
            try:
                async for message in websocket:
                    packets = unpack(
                        message if isinstance(message, bytes) else message.encode("utf-8")
                    )
                    for packet in packets:
                        if packet.operation == Operation.HEARTBEAT:
                            self.heartbeats += 1
                            if not self.silent_heartbeat:
                                await websocket.send(self._heartbeat_reply())
            finally:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
        except Exception:  # noqa: BLE001 - 假服务端不需要因客户端断开而报错
            pass

    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_auth(raw: bytes | str) -> dict[str, Any] | None:
        data = raw if isinstance(raw, bytes) else raw.encode("utf-8")
        for packet in unpack(data):
            if packet.operation == Operation.AUTH:
                try:
                    return json.loads(packet.payload)
                except ValueError:
                    return None
        return None

    @staticmethod
    def _auth_reply(code: int) -> bytes:
        body = json.dumps({"code": code}, separators=(",", ":")).encode("utf-8")
        return pack(Operation.AUTH_REPLY, body, ProtoVer.HEARTBEAT)

    def _heartbeat_reply(self) -> bytes:
        import struct

        body = struct.pack(">I", self.popularity) + HEARTBEAT_PAYLOAD
        return pack(Operation.HEARTBEAT_REPLY, body, ProtoVer.HEARTBEAT)

    def _notification_frame(self, message: dict[str, Any]) -> bytes:
        """按真实协议组包。

        注意：zlib/brotli 压缩的是**带 16 字节头的包**，不是裸 JSON ——
        实测 `protover=3` 解压后得到的是一串头部完整的 protover=0 包，
        因此客户端必须递归拆包。这里若压裸 JSON，客户端会正确地报「包长越界」。
        """
        body = json.dumps(message, separators=(",", ":")).encode("utf-8")
        inner = pack(Operation.MESSAGE, body, ProtoVer.PLAIN)
        if self.compress:
            return pack(Operation.MESSAGE, zlib.compress(inner), ProtoVer.ZLIB)
        return inner

    async def _push_notifications(self, websocket: Any) -> None:
        sent = 0
        for raw in self.raw_frames:
            await asyncio.sleep(self.notify_delay)
            await websocket.send(raw)
        for message in self.notifications:
            await asyncio.sleep(self.notify_delay)
            await websocket.send(self._notification_frame(message))
            sent += 1
            self.pushed = sent
            if self.drop_after is not None and sent >= self.drop_after:
                await websocket.close()
                return
        # 推完脚本后按需保持连接：读端可能还在断言它刚收到的帧。
        # linger 与"是否推过消息"无关 —— 空脚本也要能指定保持时长。
        if self.linger > 0:
            await asyncio.sleep(self.linger)
