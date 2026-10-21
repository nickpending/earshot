"""The pipeline: window untranscribed pages, transcribe, diarize, link speakers, commit.

DESIGN.md, 'earshotd', Pipeline steps 1-6. One window is committed in one transaction: its
segments, the day's speaker centroids, and the transcribed mark on its pages. Imported Limitless
audio takes the same steps as source 'limitless', and only when no pendant window is ready.
"""

from __future__ import annotations

import bisect
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Protocol

import av
import numpy as np

from . import linking, queries, records, store, windowing
from .audio import MODEL_RATE, decode_opus
from .config import Config
from .diarize import Diarization, Turn, assign_speakers
from .transcribe import Segment

log = logging.getLogger("earshotd.pipeline")

SOURCE = "pendant"
IMPORT_SOURCE = "limitless"
IMPORT_PAGE_FRAMES = 50  # imported audio is windowed on a grid of one-second "pages"
FAR_FUTURE_MS = 2**62  # imported audio is long finished: no cut is "still arriving"
SAMPLES_PER_FRAME = MODEL_RATE * windowing.FRAME_MS // 1000
PageKey = tuple[int, int, int]  # session, page_index, page_ts_ms
# One load holds at most twice the frames of a LOAD_MS range, and the bytes of that many frames
# at the largest size a stored frame may have (plus its 2-byte length). Fixed, not configured.
MAX_LOAD_FRAMES = 2 * windowing.LOAD_MS // windowing.FRAME_MS
MAX_LOAD_BYTES = MAX_LOAD_FRAMES * (2 + records.FRAME_MAX)


class Models(Protocol):
    name: str  # recorded in segments.model

    def transcribe(self, audio: np.ndarray) -> list[Segment]: ...

    def diarize(self, audio: np.ndarray) -> Diarization: ...


class LocalModels:
    """faster-whisper and pyannote, loaded on first use from the paths in the config."""

    def __init__(self, config: Config, threads: int | None = None) -> None:
        self.name = config.whisper_model
        self._config = config
        self._threads = threads
        self._whisper = None
        self._diarizer = None

    def transcribe(self, audio: np.ndarray) -> list[Segment]:
        from . import transcribe

        if self._whisper is None:
            self._whisper = transcribe.load_model(self._config.whisper_model)
        return transcribe.transcribe(self._whisper, audio)

    def diarize(self, audio: np.ndarray) -> Diarization:
        from . import diarize

        if self._diarizer is None:
            self._diarizer = diarize.load_diarizer(
                self._config.diarization_model, self._threads or diarize.DEFAULT_THREADS
            )
        return diarize.diarize(self._diarizer, audio)


@dataclass(frozen=True)
class StoredPage:
    session: int
    page_index: int
    ts_ms: int
    frames: list[bytes]

    @property
    def key(self) -> PageKey:
        return (self.session, self.page_index, self.ts_ms)

    @property
    def duration_ms(self) -> int:
        return len(self.frames) * windowing.FRAME_MS


@dataclass(frozen=True)
class SegmentRow:
    start_ms: int
    end_ms: int
    text: str
    speaker: str | None
    name: str | None


@dataclass(frozen=True)
class WindowResult:
    day: str
    keys: list[PageKey]
    first_ms: int  # wall time the window's audio starts
    last_ms: int  # wall time it ends
    segments: list[SegmentRow]
    # Turns (overlap kept) labelled with day speakers, in ms from the window's first sample.
    day_turns: list[Turn] = field(default_factory=list)
    reason: str = ""  # why the window closed, by windowing.plan_window
    source: str = SOURCE


class Layout:
    """Where each page's audio sits in a window's samples, and back to wall time."""

    def __init__(self, pages: list[StoredPage]) -> None:
        self.pages = pages
        self.starts: list[int] = []  # audio ms at the start of each page
        at = 0
        for p in pages:
            self.starts.append(at)
            at += p.duration_ms
        self.total_ms = at

    def wall(self, audio_ms: int, *, end: bool = False) -> int:
        """Wall time of an offset; an end offset belongs to the page its last sample is in."""
        probe = min(max(audio_ms - 1 if end else audio_ms, 0), self.total_ms - 1)
        i = bisect.bisect_right(self.starts, probe) - 1
        p = self.pages[i]
        return min(p.ts_ms + audio_ms - self.starts[i], p.ts_ms + p.duration_ms)


def _load_pages(conn, where: str, args: tuple) -> tuple[list[StoredPage], bool]:
    """The pages matching `where` in time order, and whether the load stopped at its bound.

    The load holds at most MAX_LOAD_FRAMES frames and MAX_LOAD_BYTES bytes of stored frames, plus
    the first page whatever its size, so a caller always makes progress. True means rows were
    left behind.
    """
    pages: list[StoredPage] = []
    frames = size = 0
    cursor = conn.execute(
        "SELECT session, page_index, page_ts_ms, frames FROM pages WHERE "
        + where
        + " ORDER BY page_ts_ms, session, page_index",
        args,
    )
    for s, i, t, blob in cursor:
        split = windowing.split_frames(blob)
        if pages and (
            frames + len(split) > MAX_LOAD_FRAMES or size + len(blob) > MAX_LOAD_BYTES
        ):
            return pages, True
        pages.append(StoredPage(s, i, t, split))
        frames += len(split)
        size += len(blob)
    return pages, False


def _decode(pages: list[StoredPage]) -> np.ndarray:
    frames = [f for p in pages for f in p.frames]
    return decode_opus(frames).samples


def _decode_ogg(path: str, skip_ms: int, limit_ms: int) -> tuple[np.ndarray, bool]:
    """16 kHz mono float32 of an Ogg Opus file from `skip_ms`, at most `limit_ms` of it.

    The bool is True when the file ended inside the limit: nothing more follows the samples.
    """
    skip, limit = skip_ms * MODEL_RATE // 1000, limit_ms * MODEL_RATE // 1000
    resampler = av.AudioResampler(format="flt", layout="mono", rate=MODEL_RATE)
    chunks: list[np.ndarray] = []
    seen = kept = 0

    def take(frames) -> bool:
        nonlocal seen, kept
        for out in frames:
            a = out.to_ndarray().reshape(-1)
            drop = min(max(skip - seen, 0), len(a))
            seen += len(a)
            a = a[drop:]
            if len(a):
                chunks.append(a)
                kept += len(a)
        return kept >= limit

    ended = False
    with av.open(path) as container:
        for frame in container.decode(audio=0):
            if take(resampler.resample(frame)):
                break
        else:
            ended = True
            take(resampler.resample(None))
    samples = (
        np.clip(np.concatenate(chunks), -1.0, 1.0).astype(np.float32, copy=False)
        if chunks
        else np.zeros(0, dtype=np.float32)
    )
    return samples[:limit], ended


def _grid(start_ms: int, frames: int) -> list[StoredPage]:
    """Timing-only pages for imported audio: `frames` opus-frame slots from `start_ms`.

    The slots carry no data (the audio is decoded from the file, not from frames); they give the
    window planner and `Layout` the same page timing a pendant window has.
    """
    out = []
    for k, at in enumerate(range(0, frames, IMPORT_PAGE_FRAMES)):
        n = min(IMPORT_PAGE_FRAMES, frames - at)
        out.append(StoredPage(0, k, start_ms + at * windowing.FRAME_MS, [b""] * n))
    return out


def _window_audio(samples: np.ndarray, pages: list[StoredPage]) -> np.ndarray:
    return samples[: sum(len(p.frames) for p in pages) * SAMPLES_PER_FRAME]


def _page_infos(
    pages: list[StoredPage], samples: np.ndarray
) -> list[windowing.PageInfo]:
    mask = windowing.quiet_mask(samples, MODEL_RATE)
    infos, at = [], 0
    for p in pages:
        quiet = mask[at : at + len(p.frames)]
        if len(quiet) < len(p.frames):
            quiet = np.pad(quiet, (0, len(p.frames) - len(quiet)))
        infos.append(windowing.PageInfo(p.ts_ms, len(p.frames), quiet))
        at += len(p.frames)
    return infos


def _speech_seconds(turns: list[Turn]) -> dict[str, float]:
    out: dict[str, float] = {}
    for t in turns:
        out[t.speaker] = out.get(t.speaker, 0.0) + (t.end_ms - t.start_ms) / 1000
    return out


class Pipeline:
    def __init__(self, config: Config, models: Models) -> None:
        self.config = config
        self.models = models

    # -- windowing -----------------------------------------------------------------------

    def run_once(self, now_ms: int) -> WindowResult | None:
        """Close and process the next window if one is ready at `now_ms`; None otherwise.

        A ready pendant window always goes first; imported Limitless audio is taken only when
        none is, so live transcripts stay minutes behind while the archive drains.
        """
        return self._run_pendant(now_ms) or self._run_imported()

    def _run_pendant(self, now_ms: int) -> WindowResult | None:
        conn = store.connect(self.config.db_path)
        try:
            first = conn.execute(
                "SELECT min(page_ts_ms) FROM pages WHERE transcribed_at IS NULL"
            ).fetchone()[0]
            if first is None:
                return None
            pages, capped = _load_pages(
                conn,
                "transcribed_at IS NULL AND page_ts_ms <= ?",
                (first + windowing.LOAD_MS,),
            )
        finally:
            conn.close()
        timing = [
            windowing.PageInfo(p.ts_ms, len(p.frames), np.zeros(0, dtype=bool))
            for p in pages
        ]
        if not windowing.needs_audio(timing, now_ms, capped=capped):
            return None  # no decoding of 11 minutes of opus every poll while a stretch is open
        samples = _decode(pages)
        infos = _page_infos(pages, samples)
        plan = windowing.plan_window(infos, now_ms, capped=capped)
        if plan is None:
            return None
        window = pages[: plan.count]
        result = self.process_window(window, _window_audio(samples, window))
        return replace(result, reason=plan.reason)

    # -- imported Limitless audio ------------------------------------------------------------

    def _run_imported(self) -> WindowResult | None:
        """Process the next window of the oldest untranscribed imported chunk, if any."""
        conn = store.connect(self.config.db_path)
        try:
            row = conn.execute(
                "SELECT rowid, start_ms, path, done_ms FROM limitless_audio"
                " WHERE transcribed_at IS NULL"
                " ORDER BY start_ms, lifelog_id, rowid LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        rowid, start_ms, path, done_ms = row
        at = start_ms if done_ms is None else done_ms
        samples, ended = _decode_ogg(path, at - start_ms, windowing.LOAD_MS)
        pages = _grid(at, len(samples) // SAMPLES_PER_FRAME)
        plan = (
            windowing.plan_window(_page_infos(pages, samples), FAR_FUTURE_MS)
            if pages
            else None
        )
        count = plan.count if plan else 0
        window = pages[:count]
        # The chunk is finished when its file has ended and this window takes all that was left.
        finished = ended and count == len(pages)
        end_at = window[-1].ts_ms + window[-1].duration_ms if window else at

        def mark(conn: sqlite3.Connection) -> None:
            moved = conn.execute(
                "UPDATE limitless_audio SET done_ms = ?, transcribed_at = ?"
                " WHERE rowid = ? AND transcribed_at IS NULL"
                " AND coalesce(done_ms, start_ms) = ?",
                (end_at, _now_s() if finished else None, rowid, at),
            ).rowcount
            if moved != 1:
                raise RuntimeError("this chunk of imported audio was already processed")

        if not window:
            if not ended:
                raise RuntimeError("imported audio yielded no window before its end")
            # Nothing left to transcribe (an empty file, or under one frame of tail).
            conn = store.connect(self.config.db_path)
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    mark(conn)
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
            finally:
                conn.close()
            return self._run_imported()
        result = self.process_window(
            window, _window_audio(samples, window), IMPORT_SOURCE, mark
        )
        return replace(result, reason=plan.reason if plan else "")

    # -- one window ----------------------------------------------------------------------

    def process_window(
        self,
        pages: list[StoredPage],
        audio: np.ndarray | None = None,
        source: str = SOURCE,
        mark: Callable[[sqlite3.Connection], None] | None = None,
    ) -> WindowResult:
        """Transcribe, diarize, link and commit one window of pages.

        `mark` records the window's audio as done inside the commit transaction; the default
        marks the pages themselves.
        """
        if audio is None:
            audio = _decode(pages)
        layout = Layout(pages)
        day = queries.local_date(pages[0].ts_ms, self.config.zone).isoformat()
        segs = self.models.transcribe(audio) if len(audio) else []
        dia = self.models.diarize(audio) if segs else None
        if dia is not None and dia.embeddings is None:
            raise RuntimeError("the diarization model produced no speaker embeddings")
        assigned = assign_speakers(segs, dia.exclusive_turns) if dia else []
        return self._commit(day, pages, layout, segs, assigned, dia, source, mark)

    def _commit(
        self,
        day: str,
        pages: list[StoredPage],
        layout: Layout,
        segs: list[Segment],
        assigned: list[str | None],
        dia: Diarization | None,
        source: str = SOURCE,
        mark: Callable[[sqlite3.Connection], None] | None = None,
    ) -> WindowResult:
        conn = store.connect(self.config.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                mapping: dict[str, str] = {}
                names: dict[str, str] = {}
                if dia is not None and dia.embeddings:
                    mapping, names = self._link(conn, day, dia)
                rows = [
                    SegmentRow(
                        layout.wall(s.start_ms),
                        max(layout.wall(s.start_ms), layout.wall(s.end_ms, end=True)),
                        s.text,
                        mapping.get(a) if a else None,
                        names.get(mapping[a]) if a and a in mapping else None,
                    )
                    for s, a in zip(segs, assigned)
                ]
                conn.executemany(
                    "INSERT INTO segments (start_ms, end_ms, text, source, speaker, name, model)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            r.start_ms,
                            r.end_ms,
                            r.text,
                            source,
                            r.speaker,
                            r.name,
                            self.models.name,
                        )
                        for r in rows
                    ],
                )
                if mark is not None:
                    mark(conn)
                else:
                    done = conn.executemany(
                        "UPDATE pages SET transcribed_at = ? WHERE session = ?"
                        " AND page_index = ? AND page_ts_ms = ? AND transcribed_at IS NULL",
                        [(_now_s(), *p.key) for p in pages],
                    ).rowcount
                    if done != len(pages):
                        raise RuntimeError(
                            "a page of this window was already transcribed"
                        )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        day_turns = (
            [
                Turn(t.start_ms, t.end_ms, mapping[t.speaker])
                for t in dia.turns
                if t.speaker in mapping
            ]
            if dia is not None
            else []
        )
        last = pages[-1]
        return WindowResult(
            day,
            [p.key for p in pages],
            pages[0].ts_ms,
            last.ts_ms + last.duration_ms,
            rows,
            day_turns,
            source=source,
        )

    def _link(
        self, conn, day: str, dia: Diarization
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Day labels for the window's speakers, the day's centroids saved, and voice names."""
        assert dia.embeddings is not None
        known = {
            s: linking.unpack(c)
            for s, c in conn.execute(
                "SELECT speaker, centroid FROM day_speakers WHERE date = ?", (day,)
            )
        }
        mapping, touched = linking.link_day(
            dia.embeddings,
            _speech_seconds(dia.exclusive_turns),
            known,
            self.config.speaker_link_threshold,
        )
        conn.executemany(
            "INSERT OR REPLACE INTO day_speakers (date, speaker, centroid) VALUES (?, ?, ?)",
            [(day, s, linking.pack(c)) for s, c in touched.items()],
        )
        voices = {
            n: linking.unpack(e)
            for n, e in conn.execute("SELECT name, embedding FROM voices")
        }
        named = linking.match(touched, voices, self.config.voice_match_threshold)
        return mapping, named


def _now_s() -> int:
    return int(time.time())


class EnrollError(ValueError):
    pass


def enroll(
    config: Config,
    models: Models,
    name: str,
    from_ms: int,
    to_ms: int,
    replace: bool = False,
) -> None:
    """Store the voice of whoever speaks most in the wall-clock range [from_ms, to_ms).

    A name that is already enrolled is refused unless `replace`, so a wrong range cannot silently
    destroy a good fingerprint.
    """
    name = name.strip()
    if not name:
        raise EnrollError("a voice needs a name")
    if to_ms <= from_ms:
        raise EnrollError("--to must be after --from")
    conn = store.connect(config.db_path)
    try:
        pages, capped = _load_pages(
            conn, "page_ts_ms < ? AND page_ts_ms > ?", (to_ms, from_ms - 60_000)
        )
    finally:
        conn.close()
    if capped:
        minutes = MAX_LOAD_FRAMES * windowing.FRAME_MS // 60_000
        raise EnrollError(
            f"that range holds more than {minutes} minutes of stored audio;"
            " give a shorter one"
        )
    pages = [p for p in pages if p.ts_ms + p.duration_ms > from_ms]
    if not pages:
        raise EnrollError("no stored audio in that range")
    layout = Layout(pages)
    samples = _decode(pages)
    # Pieces of the range, page by page, so gaps between pages are not stitched as silence.
    pieces = []
    for i, p in enumerate(pages):
        s, e = max(from_ms, p.ts_ms), min(to_ms, p.ts_ms + p.duration_ms)
        if e > s:
            base = layout.starts[i] - p.ts_ms
            pieces.append(
                samples[
                    (base + s) * MODEL_RATE // 1000 : (base + e) * MODEL_RATE // 1000
                ]
            )
    audio = np.concatenate(pieces)
    dia = models.diarize(audio)
    speech = _speech_seconds(dia.exclusive_turns)
    if not speech:
        raise EnrollError("no speech found in that range")
    # The dominant speaker, whether or not the model gave them a usable fingerprint: a range
    # dominated by a voice with none must not enrol whoever spoke second.
    speaker = max(speech, key=lambda s: speech[s])
    if not dia.embeddings or speaker not in dia.embeddings:
        raise EnrollError("no usable voice was found in that range")
    vec = linking.unit(dia.embeddings[speaker])
    conn = store.connect(config.db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        taken = conn.execute("SELECT 1 FROM voices WHERE name = ?", (name,)).fetchone()
        if taken and not replace:
            conn.execute("ROLLBACK")
            raise EnrollError(
                f"{name} is already enrolled; pass --replace to overwrite"
            )
        conn.execute(
            "INSERT OR REPLACE INTO voices (name, embedding) VALUES (?, ?)",
            (name, linking.pack(vec)),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
