"""协议层单测：打包 / 拆包 / 解压 / 边界条件。"""

from __future__ import annotations

import json
import struct
import zlib

import pytest

from bili_danmaku.protocol import (
    HEADER,
    HEADER_LEN,
    HEARTBEAT_PAYLOAD,
    Operation,
    ProtoVer,
    ProtocolError,
    pack,
    parse_popularity,
    resolve_protover,
    unpack,
)


def test_pack_layout() -> None:
    payload = b'{"code":0}'
    frame = pack(Operation.AUTH_REPLY, payload)

    total, hdr_len, protover, operation, sequence = HEADER.unpack_from(frame, 0)
    assert total == HEADER_LEN + len(payload)
    assert total == len(frame)
    assert hdr_len == 16
    assert protover == ProtoVer.PLAIN
    assert operation == Operation.AUTH_REPLY
    assert sequence == 1
    assert frame[HEADER_LEN:] == payload


def test_roundtrip_plain() -> None:
    frame = pack(Operation.MESSAGE, b"hello")
    packets = unpack(frame)
    assert len(packets) == 1
    assert packets[0].operation == Operation.MESSAGE
    assert packets[0].payload == b"hello"


def test_auth_reply_real_bytes() -> None:
    """复刻实测认证回应：总长 26 / 头长 16 / protover 0 / op 8 / {"code":0}。"""
    frame = bytes.fromhex("0000001a001000010000000800000001") + b'{"code":0}'
    packets = unpack(frame)
    assert len(packets) == 1
    assert packets[0].operation == Operation.AUTH_REPLY
    assert json.loads(packets[0].payload) == {"code": 0}


def test_heartbeat_reply_popularity() -> None:
    """实测心跳回应：前 4 字节为人气值，其后回显心跳原文。"""
    body = struct.pack(">I", 2466) + HEARTBEAT_PAYLOAD
    frame = pack(Operation.HEARTBEAT_REPLY, body, ProtoVer.HEARTBEAT)
    packets = unpack(frame)
    assert len(packets) == 1
    assert parse_popularity(packets[0].payload) == 2466
    assert packets[0].payload[4:] == b"[object Object]"


def test_nested_zlib_unpacks_multiple_messages() -> None:
    """protover=2 解压后仍是一串带头部的包 —— 必须递归拆包。"""
    first = pack(Operation.MESSAGE, json.dumps({"cmd": "DANMU_MSG"}).encode())
    second = pack(Operation.MESSAGE, json.dumps({"cmd": "SEND_GIFT"}).encode())
    compressed = zlib.compress(first + second)
    frame = pack(Operation.MESSAGE, compressed, ProtoVer.ZLIB)

    packets = unpack(frame)
    assert len(packets) == 2
    assert json.loads(packets[0].payload)["cmd"] == "DANMU_MSG"
    assert json.loads(packets[1].payload)["cmd"] == "SEND_GIFT"


def test_nested_zlib_two_levels() -> None:
    inner = pack(Operation.MESSAGE, b'{"cmd":"X"}')
    middle = pack(Operation.MESSAGE, zlib.compress(inner), ProtoVer.ZLIB)
    outer = pack(Operation.MESSAGE, zlib.compress(middle), ProtoVer.ZLIB)
    packets = unpack(outer)
    assert len(packets) == 1
    assert packets[0].payload == b'{"cmd":"X"}'


@pytest.mark.skipif(
    not __import__("bili_danmaku.protocol", fromlist=["has_brotli"]).has_brotli(),
    reason="未安装 brotli",
)
def test_nested_brotli() -> None:
    import brotli

    inner = pack(Operation.MESSAGE, b'{"cmd":"Y"}')
    frame = pack(Operation.MESSAGE, brotli.compress(inner), ProtoVer.BROTLI)
    packets = unpack(frame)
    assert len(packets) == 1
    assert packets[0].payload == b'{"cmd":"Y"}'


def test_truncated_packet_raises() -> None:
    frame = pack(Operation.MESSAGE, b"0123456789")
    with pytest.raises(ProtocolError, match="包长越界"):
        unpack(frame[:-3])


def test_illegal_header_length_raises() -> None:
    frame = HEADER.pack(32, 8, 0, Operation.MESSAGE, 1) + b"x" * 16
    with pytest.raises(ProtocolError, match="非法包头长度"):
        unpack(frame)


def test_packet_length_smaller_than_header_raises() -> None:
    frame = HEADER.pack(8, 16, 0, Operation.MESSAGE, 1)
    with pytest.raises(ProtocolError, match="小于头长"):
        unpack(frame)


def test_trailing_bytes_raise() -> None:
    frame = pack(Operation.MESSAGE, b"abc") + b"\x00\x01\x02"
    with pytest.raises(ProtocolError, match="尾部残留"):
        unpack(frame)


def test_empty_input_yields_no_packets() -> None:
    assert unpack(b"") == []


def test_corrupt_zlib_raises_protocol_error() -> None:
    frame = pack(Operation.MESSAGE, b"not-zlib-data", ProtoVer.ZLIB)
    with pytest.raises(ProtocolError, match="zlib 解压失败"):
        unpack(frame)


def test_large_payload_packs_and_unpacks() -> None:
    """1 MiB 负载应正确封包（长度字段是 uint32，上限约 4 GiB）。"""
    payload = b"x" * (1 << 20)
    frame = pack(Operation.MESSAGE, payload)
    assert len(frame) == HEADER_LEN + (1 << 20)
    assert unpack(frame)[0].payload == payload


def test_header_struct_is_16_bytes() -> None:
    assert HEADER.size == HEADER_LEN == 16


def test_parse_popularity_short_payload() -> None:
    assert parse_popularity(b"\x01\x02") is None


def test_resolve_protover_keeps_supported_version() -> None:
    assert resolve_protover(2) == 2
