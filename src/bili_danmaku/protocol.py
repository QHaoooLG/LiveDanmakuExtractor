"""B 站直播信息流二进制协议：封包 / 拆包 / 解压。

封包格式（大端序，实测确认）：

    偏移  长度  类型      含义
    0     4    uint32   包总长度（含 16 字节头）
    4     2    uint16   包头长度，固定 16
    6     2    uint16   协议版本 protover
    8     4    uint32   操作码 operation
    12    4    uint32   序列号
    16    ..   bytes    负载

protover:  0 未压缩 JSON | 1 心跳 | 2 zlib | 3 brotli
            注意 protover=2/3 解压后得到的仍是一串「带头部的包」，
            因此必须递归拆包，这是必需而非优化。
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from enum import IntEnum

HEADER = struct.Struct(">IHHII")
HEADER_LEN = 16

#: 实测 B 站网页端心跳负载就是这串字面量
HEARTBEAT_PAYLOAD = b"[object Object]"


class Operation(IntEnum):
    HEARTBEAT = 2
    HEARTBEAT_REPLY = 3
    MESSAGE = 5
    AUTH = 7
    AUTH_REPLY = 8


class ProtoVer(IntEnum):
    PLAIN = 0
    HEARTBEAT = 1
    ZLIB = 2
    BROTLI = 3


class ProtocolError(Exception):
    """封包结构非法。"""


class CompressionUnavailable(ProtocolError):
    """收到 brotli 压缩包但未安装 brotli 依赖。"""


@dataclass(frozen=True, slots=True)
class Packet:
    protover: int
    operation: int
    sequence: int
    payload: bytes


_brotli: object | None = None
_brotli_checked = False


def has_brotli() -> bool:
    """探测 brotli 是否可用（结果缓存）。"""
    global _brotli, _brotli_checked
    if not _brotli_checked:
        _brotli_checked = True
        try:
            import brotli  # type: ignore[import-not-found]

            _brotli = brotli
        except Exception:  # noqa: BLE001
            _brotli = None
    return _brotli is not None


def resolve_protover(requested: int) -> int:
    """按依赖可用性把声明的 protover 降到可处理的版本。"""
    if requested == ProtoVer.BROTLI and not has_brotli():
        return int(ProtoVer.ZLIB)
    return int(requested)


def pack(
    operation: int,
    payload: bytes,
    protover: int = ProtoVer.PLAIN,
    sequence: int = 1,
) -> bytes:
    """打包。不变量：总长 == HEADER_LEN + len(payload)。"""
    total = HEADER_LEN + len(payload)
    if total > 0xFFFFFFFF:
        raise ProtocolError("负载过大，超出 uint32 长度上限")
    return HEADER.pack(total, HEADER_LEN, int(protover), int(operation), sequence) + payload


def unpack(data: bytes) -> list[Packet]:
    """拆包并递归解压嵌套包。

    抛出 ProtocolError（结构非法）或 CompressionUnavailable（缺 brotli），
    绝不静默截断数据。
    """
    packets: list[Packet] = []
    offset = 0
    total = len(data)

    while offset + HEADER_LEN <= total:
        pkt_len, hdr_len, protover, operation, sequence = HEADER.unpack_from(data, offset)

        if hdr_len < HEADER_LEN:
            raise ProtocolError(f"非法包头长度 {hdr_len}")
        if pkt_len < hdr_len:
            raise ProtocolError(f"包长 {pkt_len} 小于头长 {hdr_len}")
        if offset + pkt_len > total:
            raise ProtocolError(f"包长越界: 需 {pkt_len}，仅剩 {total - offset}")

        body = data[offset + hdr_len : offset + pkt_len]

        if protover == ProtoVer.ZLIB:
            try:
                decompressed = zlib.decompress(body)
            except zlib.error as exc:
                raise ProtocolError(f"zlib 解压失败: {exc}") from exc
            packets.extend(unpack(decompressed))
        elif protover == ProtoVer.BROTLI:
            if not has_brotli():
                raise CompressionUnavailable(
                    "收到 brotli 压缩包但未安装 brotli 依赖，"
                    "请 pip install brotli 或在配置中把 protover 设为 2"
                )
            assert _brotli is not None
            decompressed = _brotli.decompress(body)  # type: ignore[attr-defined]
            packets.extend(unpack(decompressed))
        else:
            packets.append(Packet(protover, operation, sequence, body))

        offset += pkt_len

    if offset != total:
        raise ProtocolError(f"尾部残留 {total - offset} 字节无法解析")

    return packets


def parse_popularity(payload: bytes) -> int | None:
    """从心跳回应负载解析人气值（前 4 字节大端 uint32）。"""
    if len(payload) < 4:
        return None
    return struct.unpack(">I", payload[:4])[0]
