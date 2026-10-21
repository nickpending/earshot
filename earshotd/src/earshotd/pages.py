"""Parsers for the upload bodies: pages.v1 (parsed pages) and pages.v2 (raw StorageBuffers).

DESIGN.md, Upload contract.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

CONTENT_TYPE = "application/vnd.earshot.pages.v1"
CONTENT_TYPE_V2 = "application/vnd.earshot.pages.v2"
MAX_BODY = 4 << 20
_V2_LEN = struct.Struct("<I")
_HEADER = struct.Struct("<IIQH")
_LEN = struct.Struct("<H")
_MAX_TS = 2**63 - 1  # SQLite integers are signed 64-bit


class MalformedBody(ValueError):
    pass


@dataclass(frozen=True)
class Page:
    session: int
    page_index: int
    page_ts_ms: int
    frames: bytes  # frame records as received: (u16 len, len bytes) * frame_count
    frame_count: int


def parse(body: bytes) -> list[Page]:
    pages: list[Page] = []
    off, end = 0, len(body)
    while off < end:
        if end - off < _HEADER.size:
            raise MalformedBody(f"truncated page header at byte {off}")
        session, page_index, ts, count = _HEADER.unpack_from(body, off)
        if ts > _MAX_TS:
            raise MalformedBody(f"page timestamp out of range at byte {off}")
        off += _HEADER.size
        start = off
        for _ in range(count):
            if end - off < _LEN.size:
                raise MalformedBody(f"truncated frame length at byte {off}")
            (n,) = _LEN.unpack_from(body, off)
            off += _LEN.size
            if end - off < n:
                raise MalformedBody(f"truncated frame at byte {off}")
            off += n
        pages.append(Page(session, page_index, ts, body[start:off], count))
    return pages


def parse_v2(body: bytes) -> list[bytes]:
    """Split a v2 body into raw StorageBuffers: back to back {u32 LE length, that many bytes}."""
    out: list[bytes] = []
    off, end = 0, len(body)
    while off < end:
        if end - off < _V2_LEN.size:
            raise MalformedBody(f"truncated length at byte {off}")
        (n,) = _V2_LEN.unpack_from(body, off)
        off += _V2_LEN.size
        if end - off < n:
            raise MalformedBody(f"truncated StorageBuffer at byte {off}")
        out.append(body[off : off + n])
        off += n
    return out
