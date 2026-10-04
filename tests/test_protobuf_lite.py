"""通用 protobuf 解析器单测。"""

from __future__ import annotations

import base64

import pytest

from bili_danmaku.protobuf_lite import (
    Field,
    ProtobufError,
    decode,
    decode_base64,
    extract_strings,
    find_varints,
    parse,
    read_varint,
    to_plain,
)


# --------------------------------------------------------------------------- #
# 编码辅助（用于构造测试数据）
# --------------------------------------------------------------------------- #


def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def tag(number: int, wire: int) -> bytes:
    return varint((number << 3) | wire)


def length_delimited(number: int, payload: bytes) -> bytes:
    return tag(number, 2) + varint(len(payload)) + payload


# --------------------------------------------------------------------------- #
# varint
# --------------------------------------------------------------------------- #


def test_read_varint_single_byte() -> None:
    assert read_varint(b"\x05", 0) == (5, 1)


def test_read_varint_multi_byte() -> None:
    data = varint(300)
    value, pos = read_varint(data, 0)
    assert value == 300
    assert pos == len(data)


def test_read_varint_truncated_raises() -> None:
    with pytest.raises(ProtobufError, match="截断"):
        read_varint(b"\x80\x80", 0)


def test_read_varint_too_long_raises() -> None:
    with pytest.raises(ProtobufError, match="64 位"):
        read_varint(b"\x80" * 12, 0)


# --------------------------------------------------------------------------- #
# parse
# --------------------------------------------------------------------------- #


def test_parse_varint_field() -> None:
    fields = parse(tag(1, 0) + varint(150))
    assert fields == [Field(1, 0, 150)]


def test_parse_string_field() -> None:
    fields = parse(length_delimited(2, "你好".encode()))
    assert fields[0].number == 2
    assert fields[0].value.decode("utf-8") == "你好"


def test_parse_multiple_fields() -> None:
    data = tag(1, 0) + varint(7) + length_delimited(2, b"abc") + tag(3, 0) + varint(9)
    fields = parse(data)
    assert [f.number for f in fields] == [1, 2, 3]


def test_parse_rejects_zero_field_number() -> None:
    with pytest.raises(ProtobufError, match="字段号 0"):
        parse(varint(0) + varint(1))


def test_parse_rejects_unsupported_wire_type() -> None:
    with pytest.raises(ProtobufError, match="wire type"):
        parse(tag(1, 3) + b"\x00")


def test_parse_rejects_length_overflow() -> None:
    with pytest.raises(ProtobufError, match="越界"):
        parse(tag(1, 2) + varint(99) + b"short")


def test_parse_rejects_truncated_fixed64() -> None:
    with pytest.raises(ProtobufError, match="64 位"):
        parse(tag(1, 1) + b"\x00\x01")


def test_parse_empty_input() -> None:
    assert parse(b"") == []


# --------------------------------------------------------------------------- #
# decode（递归还原嵌套消息）
# --------------------------------------------------------------------------- #


def test_decode_nests_real_message() -> None:
    inner = length_delimited(2, "hi".encode())
    fields = decode(length_delimited(1, inner))
    assert fields[0].is_message
    assert fields[0].value[0].value == b"hi"


def test_decode_does_not_mistake_plain_text_for_message() -> None:
    """纯文本 "hi" 恰好能按 protobuf 解析，但不得被当成子消息。"""
    fields = decode(length_delimited(1, b"hi"))
    assert not fields[0].is_message
    assert fields[0].value == b"hi"


def test_decode_keeps_url_as_string() -> None:
    url = b"https://i0.hdslb.com/bfs/face/abc.jpg"
    fields = decode(length_delimited(2, url))
    assert not fields[0].is_message
    assert fields[0].value == url


def test_decode_respects_max_depth() -> None:
    """深度超过上限时不再递归，但绝不抛异常。"""
    payload = length_delimited(2, b"deep")
    for _ in range(20):
        payload = length_delimited(1, payload)
    fields = decode(payload)
    assert fields  # 能拿到顶层即可


# --------------------------------------------------------------------------- #
# base64 入口
# --------------------------------------------------------------------------- #


def test_decode_base64_roundtrip() -> None:
    raw = base64.b64encode(tag(6, 0) + varint(804456)).decode()
    fields = decode_base64(raw)
    assert fields is not None
    assert fields[0].value == 804456


def test_decode_base64_rejects_garbage() -> None:
    assert decode_base64("!!!not base64!!!") is None


def test_decode_base64_rejects_empty() -> None:
    assert decode_base64("") is None


def test_decode_base64_returns_none_on_invalid_wire_format() -> None:
    raw = base64.b64encode(varint(0) + varint(1)).decode()
    assert decode_base64(raw) is None


# --------------------------------------------------------------------------- #
# 提取辅助
# --------------------------------------------------------------------------- #


def test_extract_strings_recurses() -> None:
    inner = length_delimited(1, "内层".encode())
    data = length_delimited(2, "外层".encode()) + length_delimited(3, inner)
    # 必须用 decode()（递归还原嵌套）；parse() 只解一层
    assert extract_strings(decode(data)) == ["外层", "内层"]


def test_extract_strings_skips_binary() -> None:
    data = length_delimited(1, b"\xff\xfe\x00\x01")
    assert extract_strings(parse(data)) == []


def test_find_varints_recurses() -> None:
    inner = tag(7, 0) + varint(300)  # 含非 ASCII 字节，可被判定为嵌套消息
    data = tag(7, 0) + varint(1) + length_delimited(9, inner)
    assert find_varints(decode(data), 7) == [1, 300]


def test_ambiguous_payload_is_kept_as_bytes() -> None:
    """payload 恰好是纯可打印 ASCII 时无法判定为消息，保守保留为字节串。

    这是启发式的固有取舍：宁可少解一层，也不要把 URL / 昵称误当成子消息
    （实测中 `宅***`、`https://...` 都会走到这个分支）。
    """
    ambiguous = tag(7, 0) + varint(42)  # 字节为 b"8*"，可打印
    fields = decode(length_delimited(9, ambiguous))
    assert not fields[0].is_message
    assert fields[0].value == ambiguous


def test_to_plain_marks_field_types() -> None:
    inner = length_delimited(1, "x".encode())
    data = tag(1, 0) + varint(5) + length_delimited(2, inner) + length_delimited(3, b"\xff\x00")
    plain = to_plain(decode(data))
    assert plain[0] == {"field": 1, "type": "varint", "value": 5}
    assert plain[1]["type"] == "message"
    assert plain[2]["type"] == "bytes"
