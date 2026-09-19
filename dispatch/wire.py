"""Minimal protobuf wire-format codec.

GTFS-Realtime feeds are protobuf. The usual route is `gtfs-realtime-bindings`,
which drags in the protobuf runtime and a compiled descriptor. We decode the
wire format directly instead: the encoding is simple, and a hackathon laptop
that can run `python3` can then run this with nothing installed.

The wire format only carries field *numbers*, not names. This module returns
raw numbered fields; `gtfsrt.py` maps numbers to names using the published
gtfs-realtime.proto schema.

Wire types we handle (all that GTFS-RT uses):
    0  varint            int32/int64/uint32/uint64/bool/enum
    1  fixed64           double
    2  length-delimited  string/bytes/embedded message/packed repeated
    5  fixed32           float
"""

from __future__ import annotations

import struct
from typing import Any, Iterator

VARINT = 0
FIXED64 = 1
LENGTH = 2
FIXED32 = 5


class WireError(ValueError):
    """Malformed protobuf input."""


# --------------------------------------------------------------------------
# decoding
# --------------------------------------------------------------------------


def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """Return (value, new_pos). Varints are 7 bits per byte, little-endian,
    high bit set means 'another byte follows'."""
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise WireError("truncated varint")
        if shift > 63:
            raise WireError("varint too long")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def _zigzag(n: int) -> int:
    """Undo zigzag encoding used by sint32/sint64."""
    return (n >> 1) ^ -(n & 1)


def as_signed(n: int, bits: int = 64) -> int:
    """Reinterpret an unsigned varint as two's-complement signed.

    protobuf encodes negative int32/int64 as 64-bit unsigned varints, so a
    delay of -300 arrives as 18446744073709551316.
    """
    limit = 1 << (bits - 1)
    return n - (1 << bits) if n >= limit else n


def iter_fields(buf: bytes) -> Iterator[tuple[int, int, Any]]:
    """Yield (field_number, wire_type, raw_value) for one message body.

    raw_value is an int for varint/fixed types and bytes for length-delimited.
    Unknown field numbers still come through, which is what lets a feed add
    fields without breaking us.
    """
    pos = 0
    end = len(buf)
    while pos < end:
        key, pos = read_varint(buf, pos)
        field_no = key >> 3
        wire_type = key & 0x07
        if field_no == 0:
            raise WireError("field number 0 is not legal")
        if wire_type == VARINT:
            value, pos = read_varint(buf, pos)
        elif wire_type == FIXED64:
            if pos + 8 > end:
                raise WireError("truncated fixed64")
            value = struct.unpack_from("<Q", buf, pos)[0]
            pos += 8
        elif wire_type == FIXED32:
            if pos + 4 > end:
                raise WireError("truncated fixed32")
            value = struct.unpack_from("<I", buf, pos)[0]
            pos += 4
        elif wire_type == LENGTH:
            length, pos = read_varint(buf, pos)
            if pos + length > end:
                raise WireError("truncated length-delimited field")
            value = buf[pos : pos + length]
            pos += length
        else:
            # wire types 3/4 are deprecated groups; 6/7 are illegal.
            raise WireError(f"unsupported wire type {wire_type}")
        yield field_no, wire_type, value


def as_float(raw: int) -> float:
    return struct.unpack("<f", struct.pack("<I", raw))[0]


def as_double(raw: int) -> float:
    return struct.unpack("<d", struct.pack("<Q", raw))[0]


def as_string(raw: bytes) -> str:
    # Feeds occasionally carry latin-1 bytes in vehicle labels; never crash on it.
    return raw.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------
# encoding -- only used to build test fixtures and replayable recordings
# --------------------------------------------------------------------------


def write_varint(value: int) -> bytes:
    if value < 0:
        value += 1 << 64  # two's complement, matching protobuf for negative ints
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _key(field_no: int, wire_type: int) -> bytes:
    return write_varint((field_no << 3) | wire_type)


def enc_varint(field_no: int, value: int) -> bytes:
    return _key(field_no, VARINT) + write_varint(value)


def enc_bool(field_no: int, value: bool) -> bytes:
    return enc_varint(field_no, 1 if value else 0)


def enc_float(field_no: int, value: float) -> bytes:
    return _key(field_no, FIXED32) + struct.pack("<f", value)


def enc_double(field_no: int, value: float) -> bytes:
    return _key(field_no, FIXED64) + struct.pack("<d", value)


def enc_bytes(field_no: int, value: bytes) -> bytes:
    return _key(field_no, LENGTH) + write_varint(len(value)) + value


def enc_string(field_no: int, value: str) -> bytes:
    return enc_bytes(field_no, value.encode("utf-8"))


def enc_message(field_no: int, body: bytes) -> bytes:
    return enc_bytes(field_no, body)
