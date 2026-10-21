"""Replace the opus frames in fixtures/pendant-pages with one synthetic silent frame.

Run (from the repository root):

    uv run --project earshotd python scripts/silence-pendant-audio.py [DIR] [--check]

DIR defaults to fixtures/pendant-pages. Every *.storagebuffer.hex page there has each opus frame
(the frame rule is earshotd.records', never a copy) replaced by SILENT_FRAME, a 41-byte CELT frame
that decodes to silence; the length prefixes on the path to each frame are rewritten and every other
byte stays as captured. A page is written only after records.decode gives the same session, page
index, page timestamp, record list and frame count for the rewrite as for the original; otherwise
the script exits 1, names the page and what differs, and leaves the file untouched. --check writes
nothing and exits 1 when a page would change.

A newly captured page that carries audio is run through this script before it is committed.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from earshotd import records
from earshotd.windowing import split_frames

SILENT_FRAME = bytes.fromhex("b8" + "ff" * 40)
DEFAULT_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "pendant-pages"

# (field number, wire type, field start, length-prefix start, payload start, field end)
_Span = tuple[int, int, int, int, int, int]


def _spans(buf: bytes) -> list[_Span]:
    """Fields of buf with byte offsets; raises records.DecodeError on malformed bytes."""
    out: list[_Span] = []
    off = 0
    for num, wire, _ in records.walk(buf):
        start = off
        _, off = records._varint(buf, off)
        if wire == 0:
            _, end = records._varint(buf, off)
            out.append((num, wire, start, off, end, end))
        elif wire == 2:
            n, payload = records._varint(buf, off)
            end = payload + n
            out.append((num, wire, start, off, payload, end))
        else:
            end = off + (8 if wire == 1 else 4)
            out.append((num, wire, start, off, end, end))
        off = end
    return out


def _is_frame(val: bytes) -> bool:
    return records._FRAME_MIN <= len(val) <= records.FRAME_MAX and val[0] in records._TOC


def _map(buf: bytes, change: Callable[[int, bytes], bytes | None]) -> bytes:
    """Rebuild buf; `change` returns new bytes for a length-delimited field, None to keep it."""
    out = bytearray()
    off = 0
    for num, wire, start, length_at, payload_at, end in _spans(buf):
        out += buf[off:start]
        off = end
        old = buf[payload_at:end]
        new = change(num, old) if wire == 2 else None
        if new is None or new == old:
            out += buf[start:end]
        else:
            out += buf[start:length_at] + records._varint_bytes(len(new)) + new
    out += buf[off:]
    return bytes(out)


def _silence_audio(buf: bytes) -> bytes | None:
    """Mirror records._frames: frames sit in length-delimited fields, nested to any depth.

    None: buf is not a nested message (opaque bytes), so it keeps its bytes.
    """

    def change(_num: int, val: bytes) -> bytes | None:
        return SILENT_FRAME if _is_frame(val) else _silence_audio(val)

    try:
        return _map(buf, change)
    except records.DecodeError:
        return None


def _silence_wrapper(buf: bytes) -> bytes:
    return _map(buf, lambda num, val: _silence_audio(val) if num == 2 else None)


def _silence_flash(buf: bytes) -> bytes:
    return _map(buf, lambda num, val: _silence_wrapper(val) if num == 3 else None)


def rewrite(raw: bytes) -> bytes:
    """One StorageBuffer with every opus frame replaced by SILENT_FRAME."""
    return _map(raw, lambda num, val: _silence_flash(val) if num == 6 else None)


def differences(original: bytes, rewritten: bytes) -> list[str]:
    """What records.decode sees differently, other than the frames' content."""
    a, b = records.decode(original), records.decode(rewritten)
    found: list[str] = []
    if (a.session, a.page_index) != (b.session, b.page_index):
        found.append(
            f"session/page index {(a.session, a.page_index)} -> {(b.session, b.page_index)}"
        )
    if (a.audio is None) != (b.audio is None):
        found.append("audio present in only one")
    elif a.audio is not None and b.audio is not None:
        if a.audio.page_ts_ms != b.audio.page_ts_ms:
            found.append(f"page timestamp {a.audio.page_ts_ms} -> {b.audio.page_ts_ms}")
        if a.audio.frame_count != b.audio.frame_count:
            found.append(f"frame count {a.audio.frame_count} -> {b.audio.frame_count}")
        if any(fr != SILENT_FRAME for fr in split_frames(b.audio.frames)):
            found.append("a frame is not the silent frame")
    if a.records != b.records:
        found.append(f"record list {len(a.records)} records -> {len(b.records)} records")
    return found


def main(argv: list[str]) -> int:
    check = "--check" in argv
    args = [a for a in argv if a != "--check"]
    directory = Path(args[0]) if args else DEFAULT_DIR
    status = 0
    for path in sorted(directory.glob("*.storagebuffer.hex")):
        original = bytes.fromhex(path.read_text().strip())
        rewritten = rewrite(original)
        if rewritten == original:
            continue
        problems = differences(original, rewritten)
        if problems:
            print(f"{path.name}: rewrite differs: {'; '.join(problems)}", file=sys.stderr)
            status = 1
        elif check:
            print(f"{path.name}: would change", file=sys.stderr)
            status = 1
        else:
            path.write_text(rewritten.hex() + "\n")
            print(f"{path.name}: {len(original)} -> {len(rewritten)} bytes")
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
