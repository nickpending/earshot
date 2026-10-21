"""Window closing: which untranscribed pages are cut into the next window, and when.

Pure logic over page timing and a per-frame quiet mask; no database, no models, no clock of its
own (DESIGN.md, Pipeline step 1).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

FRAME_MS = 20  # one opus frame
WINDOW_MS = 600_000  # a window closes at about this length
TAIL_MS = 60_000  # the cut is the longest silence in the last stretch of this length
SETTLE_MS = 120_000  # a cut must be this old before its window is transcribed
GAP_MS = 120_000  # a gap in page timestamps longer than this always closes a window
LOAD_MS = WINDOW_MS + 60_000  # how far past the first page the caller must load pages

_LEN = struct.Struct("<H")


def split_frames(blob: bytes) -> list[bytes]:
    """The opus frames of a stored page (`(u16 len, len bytes) * n`)."""
    frames: list[bytes] = []
    off = 0
    while off < len(blob):
        (n,) = _LEN.unpack_from(blob, off)
        off += _LEN.size
        frames.append(blob[off : off + n])
        off += n
    return frames


@dataclass(frozen=True)
class PageInfo:
    ts_ms: int
    frame_count: int
    quiet: np.ndarray  # bool per frame: True where the frame is below the silence level

    @property
    def duration_ms(self) -> int:
        return self.frame_count * FRAME_MS

    @property
    def end_ms(self) -> int:
        return self.ts_ms + self.duration_ms


def quiet_mask(samples: np.ndarray, rate: int) -> np.ndarray:
    """Per-frame quiet flags for decoded audio; the level adapts to the recording's noise floor."""
    n = rate * FRAME_MS // 1000
    frames = len(samples) // n
    if frames == 0:
        return np.zeros(0, dtype=bool)
    rms = np.sqrt((samples[: frames * n].reshape(frames, n) ** 2).mean(axis=1))
    return rms < max(0.002, 1.5 * float(np.percentile(rms, 10)))


def _quiet_runs(pages: list[PageInfo], lo: int, hi: int) -> list[tuple[int, int]]:
    """Merged quiet intervals in [lo, hi]: quiet frames, and gaps between pages."""
    runs: list[list[int]] = []

    def add(s: int, e: int) -> None:
        s, e = max(s, lo), min(e, hi)
        if e <= s:
            return
        if runs and s <= runs[-1][1]:
            runs[-1][1] = max(runs[-1][1], e)
        else:
            runs.append([s, e])

    for i, p in enumerate(pages):
        if i and p.ts_ms > pages[i - 1].end_ms:
            add(pages[i - 1].end_ms, p.ts_ms)
        if p.end_ms < lo or p.ts_ms > hi:
            continue
        for k, q in enumerate(p.quiet[: p.frame_count]):
            if q:
                add(p.ts_ms + k * FRAME_MS, p.ts_ms + (k + 1) * FRAME_MS)
    return [(s, e) for s, e in runs]


def _cut(pages: list[PageInfo], start: int) -> int:
    """Index of the first page after the window: the boundary at the longest silence in the tail."""
    hi = start + WINDOW_MS
    lo = hi - TAIL_MS
    # A boundary's time is where the next page starts, so a silent gap before it counts as
    # silence inside the window and a gap that began before the tail is still a candidate.
    bounds = [
        (i, pages[i].ts_ms) for i in range(1, len(pages)) if pages[i - 1].end_ms <= hi
    ]
    inside = [(i, t) for i, t in bounds if lo <= t <= hi]
    if not inside:
        # No page boundary in the tail (a long gap spans it): the last boundary before it.
        return bounds[-1][0] if bounds else len(pages)
    runs = _quiet_runs(pages, lo, hi)
    if not runs:
        return inside[-1][0]
    s, e = max(runs, key=lambda r: r[1] - r[0])
    centre = (s + e) // 2
    within = [(i, t) for i, t in inside if s <= t <= e] or inside
    return min(within, key=lambda b: abs(b[1] - centre))[0]


@dataclass(frozen=True)
class Plan:
    count: int  # leading pages that form the window
    # "length" (a full window), "gap" (a timestamp gap follows), "cap" (the load stopped at its
    # bound with more pages waiting) or "idle"
    reason: str


def _stretch(
    pages: list[PageInfo], now_ms: int, capped: bool = False
) -> tuple[int, str | None]:
    """The first continuous stretch: how many pages, and why it closes (None while it is open).

    The one place the gap, length, cap and idle rules live; `plan_window` and `needs_audio` both
    read it. `capped` says the caller's load stopped at its bound with more pages waiting.
    """
    n = len(pages)
    for i in range(1, len(pages)):
        if pages[i].ts_ms - pages[i - 1].end_ms > GAP_MS:
            n = i
            break
    last_end = pages[n - 1].end_ms
    if last_end - pages[0].ts_ms >= WINDOW_MS:
        return n, "length"
    if n < len(pages):
        return n, "gap"
    if capped:
        return n, "cap"
    if now_ms - last_end > GAP_MS:
        return n, "idle"
    return n, None


def needs_audio(pages: list[PageInfo], now_ms: int, *, capped: bool = False) -> bool:
    """Whether a window could be ready at `now_ms`, judged from page timing alone.

    False means `plan_window` would return None whatever the audio holds, so the caller can skip
    decoding. True is permissive: the cut may still turn out too young.
    """
    if not pages:
        return False
    n, reason = _stretch(pages, now_ms, capped)
    if reason is None:
        return False
    if reason == "length":
        # The cut is no older than the tail's start less one gap (a gap may span the tail).
        return now_ms - (pages[0].ts_ms + WINDOW_MS - TAIL_MS - GAP_MS) >= SETTLE_MS
    return now_ms - pages[n - 1].end_ms >= SETTLE_MS


def plan_window(
    pages: list[PageInfo], now_ms: int, *, capped: bool = False
) -> Plan | None:
    """The next window over the leading `pages` (untranscribed, in time order), and why it closes.

    None while no window is ready: the stretch is still open and shorter than a window, or the
    cut is not yet SETTLE_MS old. `pages` must reach LOAD_MS past the first page, or the end of
    the untranscribed pages, or stop at the caller's load bound (`capped`).
    """
    if not pages:
        return None
    n, reason = _stretch(pages, now_ms, capped)
    if reason is None:
        return None
    stretch = pages[:n]
    count = _cut(stretch, pages[0].ts_ms) if reason == "length" else n
    cut_ms = (
        stretch[count - 1].end_ms
        if count == n
        else min(stretch[count].ts_ms, stretch[count - 1].end_ms)
    )
    if now_ms - cut_ms < SETTLE_MS:
        return None
    return Plan(count, reason)


def next_window(pages: list[PageInfo], now_ms: int) -> int | None:
    """How many of the leading `pages` form the next window, or None; see `plan_window`."""
    plan = plan_window(pages, now_ms)
    return plan.count if plan else None
