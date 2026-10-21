"""Decode one pendant StorageBuffer into its audio page and its other records.

Field map: docs/protocol.md, 'Flash page' and 'Flash page records'. The decoder only reads
bytes, so `earshotd redecode` can re-run it over stored raw pages after it learns a new record.
"""

from __future__ import annotations

import json
import re
import struct
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from .pages import Page

_MAX_TS = 2**63 - 1  # SQLite integers are signed 64-bit
_TOC = frozenset((0xB8, 0x78, 0xF8, 0xB0, 0x70, 0xF0))
_FRAME_MIN, FRAME_MAX = 10, 200
_UNSET_OFFSET = 0xFFFFFFFF  # "all-ones when unset"
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_LOG = re.compile(r"^\[[^\]]*\]\s*<(\w+)>\s+([^:\s]+):\s?(.*)$", re.DOTALL)


class DecodeError(ValueError):
    pass


@dataclass(frozen=True)
class Record:
    kind: str
    ts_ms: int | None  # None: the page carries no timestamp (a CRC-failed page)
    data: dict[str, Any]

    def data_json(self) -> str:
        return json.dumps(self.data, sort_keys=True)


@dataclass(frozen=True)
class Decoded:
    session: int
    page_index: int
    audio: Page | None
    records: list[Record]


Value = int | bytes
# (field number, wire type, value): an int for varint and fixed, bytes for length-delimited.
Field = tuple[int, int, Value]
Fields = dict[int, list[Value]]


def _varint(buf: bytes, off: int) -> tuple[int, int]:
    value = shift = 0
    for i in range(off, min(off + 10, len(buf))):
        value |= (buf[i] & 0x7F) << shift
        if not buf[i] & 0x80:
            return value, i + 1
        shift += 7
    raise DecodeError(f"bad varint at byte {off}")


def walk(buf: bytes) -> Iterator[Field]:
    """Walk protobuf fields with bounds checks; raise DecodeError on malformed bytes."""
    off, end = 0, len(buf)
    while off < end:
        key, off = _varint(buf, off)
        num, wire = key >> 3, key & 7
        if num == 0:
            raise DecodeError(f"field number 0 at byte {off}")
        if wire == 0:
            val, off = _varint(buf, off)
            yield num, wire, val
        elif wire == 2:
            n, off = _varint(buf, off)
            if end - off < n:
                raise DecodeError(f"truncated field {num} at byte {off}")
            yield num, wire, buf[off : off + n]
            off += n
        elif wire in (1, 5):
            n = 8 if wire == 1 else 4
            if end - off < n:
                raise DecodeError(f"truncated field {num} at byte {off}")
            yield num, wire, int.from_bytes(buf[off : off + n], "little")
            off += n
        else:
            raise DecodeError(f"unsupported wire type {wire} at byte {off}")


def _frames(buf: bytes, out: list[bytes]) -> None:
    """Opus frames in document order: length-delimited fields of 10-200 bytes with a valid TOC."""
    try:
        for _, wire, val in walk(buf):
            if wire != 2 or not isinstance(val, bytes):
                continue
            if _FRAME_MIN <= len(val) <= FRAME_MAX and val[0] in _TOC:
                out.append(val)
            else:
                _frames(val, out)
    except DecodeError:
        return  # not a nested message: opaque bytes


def frames(audio: bytes) -> list[bytes]:
    out: list[bytes] = []
    _frames(audio, out)
    return out


def _signed(v: int) -> int:
    if v >= 2**63:
        return v - 2**64
    if 2**31 <= v < 2**32:
        return v - 2**32
    return v


def _collect(buf: bytes) -> Fields:
    out: Fields = {}
    for num, _, val in walk(buf):
        out.setdefault(num, []).append(val)
    return out


def _int(f: Fields, num: int, default: int = 0) -> int:
    v = f.get(num, [default])[0]
    if not isinstance(v, int):
        raise DecodeError(f"field {num} is not a number")
    return v


def _bytes(f: Fields, num: int) -> bytes | None:
    if num not in f:
        return None
    v = f[num][0]
    if not isinstance(v, bytes):
        raise DecodeError(f"field {num} is not length-delimited")
    return v


def _log(payload: bytes) -> dict[str, Any]:
    # Observed: {1: text}; docs/protocol.md also records a {1: {11: text}} nesting.
    raw = _bytes(_collect(payload), 1)
    if raw is not None and raw[:1] != b"[":
        try:
            raw = _bytes(_collect(raw), 11)
        except DecodeError:
            pass
    if raw is None:
        raise DecodeError("log record without text")
    text = _ANSI.sub("", raw.decode("utf-8", "replace")).rstrip("\r\n")
    m = _LOG.match(text)
    if not m:
        return {"level": None, "module": None, "text": text}
    return {"level": m.group(1), "module": m.group(2), "text": m.group(3)}


def _battery(payload: bytes) -> dict[str, Any]:
    f = _collect(payload)
    return {
        "mv": _int(f, 2),
        "ma": _signed(_int(f, 3)),
        "temp_c": _signed(_int(f, 4)),
        "pct": _int(f, 5),
    }


def _motion(payload: bytes) -> dict[str, Any]:
    f = _collect(_bytes(_collect(payload), 1) or b"")
    out: dict[str, Any] = {}
    for name, num in (("x", 1), ("y", 2), ("z", 3)):
        bits = _int(f, num)
        out[name] = round(struct.unpack("<f", bits.to_bytes(4, "little"))[0], 6)
    return out


def _thread_stats(payload: bytes) -> dict[str, Any]:
    threads = []
    extra: dict[str, int] = {}
    for num, _, val in walk(payload):
        if num == 9 and isinstance(val, bytes):
            t = _collect(val)
            threads.append(
                {
                    "name": (_bytes(t, 1) or b"").decode("utf-8", "replace"),
                    "stack_used": _int(t, 2),
                    "stack_size": _int(t, 3),
                    "cpu_pct": _int(t, 4),
                }
            )
        elif isinstance(val, int):
            extra[str(num)] = val
    return {"threads": threads, "fields": extra}


def _recording(payload: bytes) -> dict[str, Any]:
    return {"recording": bool(_int(_collect(payload), 1))}


def _bluetooth(payload: bytes) -> dict[str, Any]:
    f = _collect(payload)
    out: dict[str, Any] = {"event": _int(f, 1)}
    if 4 in f:
        out["reason"] = _int(f, 4)
    return out


def _button(payload: bytes) -> dict[str, Any]:
    # Only the press type, its counter and its time are established; the rest stays raw.
    f = _collect(payload)
    rest = {str(n): _int(f, n) for n in sorted(f) if n not in (4, 6)}
    return {
        "long_press": _int(f, 1) == 2,
        "count": _int(f, 4),
        "epoch_ms": _int(f, 6),
        "fields": rest,
    }


def _usb(payload: bytes) -> dict[str, Any]:
    return {"event": _int(_collect(payload), 1)}


def _session(payload: bytes) -> dict[str, Any]:
    f = _collect(payload)
    if _int(f, 1):
        return {"event": "start"}
    if _int(f, 2):
        return {"event": "end"}
    return {"event": None}


# wrapper field number -> (kind, decoder of its length-delimited payload)
_NAMED: dict[int, tuple[str, Callable[[bytes], dict[str, Any]]]] = {
    3: ("button", _button),
    5: ("bluetooth", _bluetooth),
    6: ("battery", _battery),
    7: ("usb", _usb),
    8: ("motion", _motion),
    9: ("recording", _recording),
    10: ("log", _log),
    11: ("thread_stats", _thread_stats),
    12: ("session", _session),
}


def _varint_bytes(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        out.append(b | (0x80 if v else 0))
        if not v:
            return bytes(out)


def _unknown(num: int, wire: int, val: Value) -> dict[str, Any]:
    if isinstance(val, bytes):
        raw = val
    elif wire == 0:
        raw = _varint_bytes(val)
    else:
        raw = val.to_bytes(8 if wire == 1 else 4, "little")
    return {"field": num, "hex": raw.hex()}


def _wrapper(
    buf: bytes, page_ts: int, out: list[Record], frame_out: list[bytes]
) -> None:
    items = list(walk(buf))
    offset = 0
    for num, wire, val in items:
        if num == 1 and wire == 0 and isinstance(val, int):
            offset = 0 if val >= _UNSET_OFFSET else val
    ts = page_ts + offset
    for num, wire, val in items:
        if num == 1 and wire == 0:
            continue
        if num == 2 and isinstance(val, bytes):
            found = frames(val)
            if found:
                frame_out.extend(found)
            else:
                out.append(Record("marker", ts, {"hex": val.hex()}))
        elif num in _NAMED and isinstance(val, bytes):
            kind, decode_one = _NAMED[num]
            try:
                out.append(Record(kind, ts, decode_one(val)))
            except DecodeError:
                # A record shape firmware changed must not reject the upload; raw is kept.
                out.append(Record("unknown", ts, _unknown(num, wire, val)))
        else:
            out.append(Record("unknown", ts, _unknown(num, wire, val)))


def decode(raw: bytes) -> Decoded:
    """Decode one StorageBuffer. Raises DecodeError when it is malformed or has no page index."""
    top = _collect(raw)
    if 5 not in top:
        raise DecodeError("StorageBuffer has no page index (field 5)")
    session, index = _int(top, 2), _int(top, 5)
    flash = _bytes(top, 6)
    records: list[Record] = []
    if not flash:
        if _int(top, 7) == 1:
            records.append(Record("crc_failed", None, {"f3": _int(top, 3), "f7": 1}))
        return Decoded(session, index, None, records)
    page_ts = 0
    wrappers: list[bytes] = []
    for num, wire, val in walk(flash):
        if num == 1 and wire == 0 and isinstance(val, int):
            page_ts = val
        elif num == 3 and isinstance(val, bytes):
            wrappers.append(val)
    if page_ts > _MAX_TS:
        raise DecodeError("page timestamp out of range")
    frame_list: list[bytes] = []
    for w in wrappers:
        _wrapper(w, page_ts, records, frame_list)
    audio = None
    if frame_list:
        blob = b"".join(len(fr).to_bytes(2, "little") + fr for fr in frame_list)
        audio = Page(session, index, page_ts, blob, len(frame_list))
    return Decoded(session, index, audio, records)
