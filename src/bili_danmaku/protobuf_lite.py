"""最小 protobuf 解析器（无第三方依赖）。

用途：`INTERACT_WORD_V2` 等新命令把数据放在 base64 编码的 protobuf 里
（实测 `data.pb`），旧版的 `uid` / `uname` 等 JSON 字段已不复存在。
这里只做通用 wire-format 解析，不依赖 .proto 定义，因此对协议演变更耐受。

不保证语义正确性 —— 字段号的含义仍需按实抓样本确认。
本模块的价值是「不丢数据 + 可检视」，而不是「猜出字段含义」。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

WIRE_VARINT = 0
WIRE_64BIT = 1
WIRE_LENGTH = 2
WIRE_32BIT = 5

MAX_DEPTH = 8


class ProtobufError(Exception):
    """数据结构不符合 protobuf wire format。"""


def read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while pos < len(data):
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ProtobufError("varint 超过 64 位")
    raise ProtobufError("varint 在数据末尾被截断")


@dataclass
class Field:
    number: int
    wire_type: int
    #: int（varint）| bytes（length/64bit/32bit）；嵌套子消息会被递归替换为 list[Field]
    value: Any

    @property
    def is_message(self) -> bool:
        return isinstance(self.value, list)


def parse(data: bytes) -> list[Field]:
    """按 wire format 解析一层，不递归。"""
    fields: list[Field] = []
    pos = 0
    while pos < len(data):
        tag, pos = read_varint(data, pos)
        number = tag >> 3
        wire_type = tag & 0x07
        if number == 0:
            raise ProtobufError(f"字段号 0 非法（偏移 {pos}）")

        if wire_type == WIRE_VARINT:
            value, pos = read_varint(data, pos)
        elif wire_type == WIRE_64BIT:
            if pos + 8 > len(data):
                raise ProtobufError("64 位字段被截断")
            value = data[pos : pos + 8]
            pos += 8
        elif wire_type == WIRE_LENGTH:
            length, pos = read_varint(data, pos)
            if length < 0 or pos + length > len(data):
                raise ProtobufError(f"长度字段越界: {length}")
            value = data[pos : pos + length]
            pos += length
        elif wire_type == WIRE_32BIT:
            if pos + 4 > len(data):
                raise ProtobufError("32 位字段被截断")
            value = data[pos : pos + 4]
            pos += 4
        else:
            raise ProtobufError(f"不支持的 wire type {wire_type}")

        fields.append(Field(number, wire_type, value))
    return fields


def _looks_like_message(payload: bytes) -> bool:
    """判断 length-delimited 负载是嵌套消息还是普通字节串。

    纯文本经常能"恰好"被解析成 protobuf（例如 "hi" → field 13 / varint），
    因此必须加判据，否则会把 URL、昵称这类字符串误当成子消息。
    采用的判据：
      1. 负载不是「完全可打印的 UTF-8」→ 含控制字符或二进制，几乎可以确定是消息；
      2. 否则要求解析出的字段号有多样性（纯文本误判通常只产生单一字段号）。
    """
    if not payload:
        return False
    try:
        nested = parse(payload)
    except ProtobufError:
        return False
    if not nested:
        return False

    try:
        fully_printable = payload.decode("utf-8").isprintable()
    except UnicodeDecodeError:
        fully_printable = False

    if not fully_printable:
        return True
    return len({field.number for field in nested}) > 1


def decode(data: bytes, *, depth: int = 0) -> list[Field]:
    """解析并尽力递归还原嵌套消息。"""
    fields = parse(data)
    if depth >= MAX_DEPTH:
        return fields
    for field in fields:
        if field.wire_type == WIRE_LENGTH and isinstance(field.value, bytes):
            if _looks_like_message(field.value):
                try:
                    field.value = decode(field.value, depth=depth + 1)
                except ProtobufError:
                    pass
    return fields


def decode_base64(raw: str) -> list[Field] | None:
    """解析 base64 编码的 protobuf。失败返回 None（调用方应保留原文）。"""
    if not raw:
        return None
    try:
        data = base64.b64decode(raw, validate=True)
    except Exception:  # noqa: BLE001 - 非法 base64 一律视为不可解析
        return None
    if not data:
        return None
    try:
        return decode(data)
    except ProtobufError:
        return None


def extract_strings(fields: list[Field], *, min_length: int = 2) -> list[str]:
    """递归提取所有可打印的 UTF-8 字符串。"""
    out: list[str] = []
    for field in fields:
        if field.is_message:
            out.extend(extract_strings(field.value, min_length=min_length))
        elif isinstance(field.value, bytes):
            try:
                text = field.value.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if len(text) >= min_length and text.isprintable():
                out.append(text)
    return out


def find_varints(fields: list[Field], number: int) -> list[int]:
    """递归收集指定字段号的 varint 值。"""
    out: list[int] = []
    for field in fields:
        if field.is_message:
            out.extend(find_varints(field.value, number))
        elif field.number == number and isinstance(field.value, int):
            out.append(field.value)
    return out


def to_plain(fields: list[Field]) -> list[dict[str, Any]]:
    """转换为可 JSON 序列化的结构，便于落盘检视。"""
    out: list[dict[str, Any]] = []
    for field in fields:
        if field.is_message:
            out.append({"field": field.number, "type": "message", "value": to_plain(field.value)})
        elif isinstance(field.value, bytes):
            try:
                text = field.value.decode("utf-8")
                printable = text.isprintable()
            except UnicodeDecodeError:
                text, printable = field.value.hex(), False
            out.append(
                {
                    "field": field.number,
                    "type": "string" if printable else "bytes",
                    "value": text,
                }
            )
        else:
            out.append({"field": field.number, "type": "varint", "value": field.value})
    return out
