"""C5: windows close at silences, only once their cut is old enough, and never twice.

The windowing rules run on their own over page timing (synthetic pages), and end to end over
pages built from the real pendant fixture with shifted timestamps. The models are a fake at the
model boundary: C5 asserts window boundaries, which no model changes.
"""

import os
import sys
from itertools import pairwise

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import numpy as np
import pytest
from conftest import FIXTURE

from earshotd import pages as pages_mod
from earshotd import store, windowing
from earshotd.config import Config
from earshotd.diarize import Diarization, Turn
from earshotd.pipeline import Pipeline
from earshotd.transcribe import Segment
from earshotd.windowing import (
    GAP_MS,
    SETTLE_MS,
    WINDOW_MS,
    PageInfo,
    next_window,
)

FRAMES = 75  # a page of 1.5 s


def page(ts: int, quiet: bool = False) -> PageInfo:
    return PageInfo(ts, FRAMES, np.full(FRAMES, quiet))


def stretch(start: int, seconds: int, silence_at: dict[int, int] | None = None):
    """Back-to-back 1.5 s pages; `silence_at` maps a second to seconds of silent pages there."""
    out, ts = [], start
    silent = set()
    for at, length in (silence_at or {}).items():
        silent |= set(range(at * 1000, (at + length) * 1000, 1500))
    while ts < start + seconds * 1000:
        out.append(page(ts, quiet=(ts - start) // 1500 * 1500 in silent))
        ts += FRAMES * windowing.FRAME_MS
    return out


T0 = 1_790_896_000_000


def test_open_stretch_shorter_than_a_window_waits():
    pages = stretch(T0, 300)
    assert next_window(pages, T0 + 300_000 + 60_000) is None


def test_idle_stretch_closes_after_the_gap_and_not_before():
    pages = stretch(T0, 300)
    end = pages[-1].end_ms
    assert next_window(pages, end + GAP_MS) is None
    assert next_window(pages, end + GAP_MS + 1) == len(pages)


def test_gap_in_page_timestamps_closes_the_window_before_the_gap():
    first = stretch(T0, 200)
    later = stretch(first[-1].end_ms + GAP_MS + 5_000, 60)
    pages = first + later
    now = later[-1].end_ms
    assert next_window(pages, now) == len(first)


def test_a_gap_of_exactly_the_limit_does_not_close():
    first = stretch(T0, 200)
    later = stretch(first[-1].end_ms + GAP_MS, 60)
    assert next_window(first + later, later[-1].end_ms) is None


def test_cut_is_the_longest_silence_in_the_last_minute():
    # 12 minutes continuous; two silences in the tail (540-600 s): 3 s at 555, 9 s at 570.
    pages = stretch(T0, 720, {555: 3, 570: 9})
    count = next_window(pages, T0 + 720_000 + SETTLE_MS)
    assert count is not None
    cut = pages[count].ts_ms - T0
    assert 570_000 <= cut <= 579_000


def test_cut_waits_until_it_is_two_minutes_old():
    pages = stretch(T0, 720, {570: 9})
    cut_estimate = T0 + 579_000
    assert next_window(pages, cut_estimate + SETTLE_MS - 10_000) is None
    assert next_window(pages, cut_estimate + SETTLE_MS + 5_000) is not None


def test_continuous_speech_cuts_at_ten_minutes():
    pages = stretch(T0, 720)
    count = next_window(pages, T0 + 720_000 + SETTLE_MS)
    assert count is not None
    assert pages[count - 1].end_ms - T0 <= WINDOW_MS + 1500
    assert pages[count - 1].end_ms - T0 >= WINDOW_MS - 3000


# -- C5 end to end ---------------------------------------------------------------------

SPAN_MS = 195_300  # the fixture's pages end this long after the first


class FakeModels:
    """The model boundary: a transcript of one segment per window and one speaker."""

    name = "fake"

    def __init__(self) -> None:
        self.windows: list[int] = []

    def transcribe(self, audio):
        self.windows.append(len(audio))
        return [Segment(0, max(len(audio) * 1000 // 16000, 1), "words")]

    def diarize(self, audio) -> Diarization:
        end = max(len(audio) * 1000 // 16000, 1)
        turns = [Turn(0, end, "SPEAKER_00")]
        return Diarization(turns, turns, {"SPEAKER_00": np.ones(8, dtype=np.float32)})


def shifted_fixture(start_offset_ms: int, length_ms: int, base: int) -> list:
    """Real fixture pages repeated back to back, timestamps shifted to run from `start_offset_ms`."""
    src = pages_mod.parse(FIXTURE.read_bytes())
    first = src[0].page_ts_ms
    out, copy = [], 0
    while copy * SPAN_MS < length_ms:
        for p in src:
            off = p.page_ts_ms - first + copy * SPAN_MS
            if off < length_ms:
                out.append(
                    pages_mod.Page(
                        p.session,
                        p.page_index,
                        base + start_offset_ms + off,
                        p.frames,
                        p.frame_count,
                    )
                )
        copy += 1
    return out


def test_continuous_stretch_gap_and_tail_are_windowed_once_each(tmp_path):
    config = Config(tokens={}, data_dir=tmp_path)
    store.init(config.db_path)
    continuous = shifted_fixture(0, 35 * 60_000, T0)
    tail = shifted_fixture(35 * 60_000 + 5 * 60_000, 3 * 60_000, T0)
    store.store_pages(config.db_path, continuous + tail)
    models = FakeModels()
    pipeline = Pipeline(config, models)

    done = []
    now = T0
    while now < T0 + 50 * 60_000:
        while (r := pipeline.run_once(now)) is not None:
            done.append((now, r))
        now += 30_000

    spans = [(r.first_ms - T0, r.last_ms - T0) for _, r in done]
    # A window runs from its first page to the cut, where the next window's first page starts.
    lengths = [
        (b.first_ms - a.first_ms) / 1000 for (_, a), (_, b) in pairwise(done[:4])
    ]
    # 35 minutes -> three windows of about ten minutes and the remainder; then the 3 minutes.
    assert len(done) == 5, spans
    for s in lengths:
        assert 520 <= s <= 605, lengths
    assert (spans[3][1] - spans[3][0]) / 1000 <= 605 and spans[4][1] - spans[4][
        0
    ] <= 185_000
    assert spans[3][1] >= 34 * 60_000 and spans[4][0] >= 40 * 60_000

    # Each cut is the longest silence in its window's last minute. The oracle reads only page
    # timestamps: the longest gap between pages whose next page starts in the tail.
    for (_, a), (_, b) in pairwise(done[:4]):
        lo, hi = a.first_ms + WINDOW_MS - 60_000, a.first_ms + WINDOW_MS
        gaps = [
            nxt.page_ts_ms - (prev.page_ts_ms + prev.frame_count * 20)
            for prev, nxt in pairwise(continuous)
            if lo <= nxt.page_ts_ms <= hi
        ]
        assert max(gaps) >= 5_000, (
            "the fixture has no silence in this tail to test against"
        )
        assert b.first_ms - a.last_ms == max(gaps), (b.first_ms - a.last_ms, gaps)

    # Each window waited until its cut was two minutes old.
    for at, r in done:
        assert at - r.last_ms >= SETTLE_MS - 100, at - r.last_ms

    # No page twice, none left.
    keys = [k for _, r in done for k in r.keys]
    assert len(keys) == len(set(keys)) == len(continuous) + len(tail)
    import sqlite3

    conn = sqlite3.connect(config.db_path)
    try:
        assert (
            conn.execute(
                "SELECT count(*) FROM pages WHERE transcribed_at IS NULL"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()
    assert len(models.windows) == 5


def test_a_window_is_never_transcribed_twice(tmp_path):
    config = Config(tokens={}, data_dir=tmp_path)
    store.init(config.db_path)
    store.store_pages(config.db_path, shifted_fixture(0, 60_000, T0))
    models = FakeModels()
    pipeline = Pipeline(config, models)
    now = T0 + 60_000 + GAP_MS + 5_000
    assert pipeline.run_once(now) is not None
    assert pipeline.run_once(now) is None
    assert pipeline.run_once(now + 3_600_000) is None
    assert len(models.windows) == 1


@pytest.mark.parametrize("n", [0])
def test_nothing_stored_nothing_to_do(tmp_path, n):
    config = Config(tokens={}, data_dir=tmp_path)
    store.init(config.db_path)
    assert Pipeline(config, FakeModels()).run_once(T0) is None


def test_worker_drains_ready_windows_with_the_injected_clock(tmp_path):
    import threading

    from earshotd import worker

    config = Config(tokens={}, data_dir=tmp_path)
    store.init(config.db_path)
    store.store_pages(config.db_path, shifted_fixture(0, 60_000, T0))
    models = FakeModels()
    pipeline = Pipeline(config, models)
    assert worker.drain(pipeline, lambda: T0 + 30_000) == 0
    stop = threading.Event()
    ticks = iter([T0 + 60_000 + GAP_MS + 5_000])

    def clock():
        try:
            return next(ticks)
        except StopIteration:
            stop.set()
            return T0 + 10**9

    worker.run(pipeline, stop, clock, poll_s=0.01)
    assert len(models.windows) == 1


def test_cut_takes_a_gap_that_began_before_the_tail():
    # Speech to 520 s, 50 s of no pages (520-570), speech again: the gap is the longest silence
    # in the tail (it ends at 570 s), so the window ends before it and the next starts after it.
    before = stretch(T0, 520)
    after = stretch(T0 + 570_000, 150)
    pages = before + after
    count = next_window(pages, T0 + 720_000 + SETTLE_MS)
    assert count == len(before)


def test_no_decoding_is_needed_while_nothing_can_close():
    from earshotd.windowing import needs_audio

    open_stretch = stretch(T0, 300)
    assert not needs_audio(open_stretch, T0 + 400_000)
    assert needs_audio(open_stretch, open_stretch[-1].end_ms + GAP_MS + 1_000)
    long_stretch = stretch(T0, 700)
    assert not needs_audio(long_stretch, T0 + 400_000)
    assert needs_audio(long_stretch, T0 + 700_000 + SETTLE_MS)


def test_an_open_stretch_is_not_decoded_on_each_poll(tmp_path, monkeypatch):
    from earshotd import pipeline as pipeline_mod

    config = Config(tokens={}, data_dir=tmp_path)
    store.init(config.db_path)
    store.store_pages(config.db_path, shifted_fixture(0, 60_000, T0))
    decoded = []
    real = pipeline_mod._decode
    monkeypatch.setattr(
        pipeline_mod, "_decode", lambda pages: decoded.append(1) or real(pages)
    )
    pipeline = Pipeline(config, FakeModels())
    assert pipeline.run_once(T0 + 90_000) is None
    assert pipeline.run_once(T0 + 100_000) is None
    assert decoded == []
    assert pipeline.run_once(T0 + 60_000 + GAP_MS + 5_000) is not None
    assert decoded  # once a window can close, the audio is decoded


def test_worker_logs_each_committed_window_without_its_text(tmp_path, caplog):
    import logging

    from earshotd import worker

    config = Config(tokens={}, data_dir=tmp_path)
    store.init(config.db_path)
    store.store_pages(config.db_path, shifted_fixture(0, 60_000, T0))
    with caplog.at_level(logging.INFO, logger="earshotd.worker"):
        assert worker.drain(Pipeline(config, FakeModels()), lambda: T0 + 400_000) == 1
    line = caplog.text
    assert "closed_by=idle" in line and "pages=" in line and "segments=1" in line
    assert "words" not in line  # the fake's transcript text


def test_worker_gives_up_after_repeated_failures_and_leaves_pages(tmp_path, caplog):
    import logging
    import threading

    from earshotd import worker

    class Broken(FakeModels):
        def transcribe(self, audio):
            self.windows.append(len(audio))
            raise RuntimeError("model blew up")

    config = Config(tokens={}, data_dir=tmp_path)
    store.init(config.db_path)
    store.store_pages(config.db_path, shifted_fixture(0, 60_000, T0))
    models = Broken()
    stop = threading.Event()
    status = worker.Status()
    assert status.state == "running"
    with caplog.at_level(logging.ERROR, logger="earshotd.worker"):
        finished = worker.run(
            Pipeline(config, models),
            stop,
            lambda: T0 + 400_000,
            poll_s=0.01,
            status=status,
        )
    assert finished is False and not stop.is_set()
    assert len(models.windows) == worker.MAX_FAILURES
    assert "worker stopping" in caplog.text
    assert (status.state, status.failures, status.error) == (
        "stalled",
        worker.MAX_FAILURES,
        "RuntimeError",
    )
    import sqlite3

    conn = sqlite3.connect(config.db_path)
    try:
        assert conn.execute(
            "SELECT count(*) FROM pages WHERE transcribed_at IS NULL"
        ).fetchone()[0] == len(shifted_fixture(0, 60_000, T0))
    finally:
        conn.close()


def read_status(config: Config, status) -> dict:
    from fastapi.testclient import TestClient

    from earshotd.app import create_app

    app = create_app(config)
    if status is not None:
        app.state.worker_status = status
    return (
        TestClient(app)
        .get("/v1/status", headers={"Authorization": "Bearer " + "b" * 64})
        .json()
    )


def test_status_tells_a_stalled_worker_from_a_backlog(tmp_path):
    from earshotd import worker

    config = Config(tokens={"b" * 64: "read"}, data_dir=tmp_path)
    store.init(config.db_path)
    store.store_pages(config.db_path, shifted_fixture(0, 60_000, T0))
    waiting = read_status(config, worker.Status())
    stalled = read_status(config, worker.Status("stalled", 3, "RuntimeError"))
    assert waiting["untranscribed_pages"] == stalled["untranscribed_pages"] > 0
    assert waiting["worker"] == {"state": "running", "failures": 0, "error": None}
    assert stalled["worker"] == {
        "state": "stalled",
        "failures": 3,
        "error": "RuntimeError",
    }
    assert read_status(config, None)["worker"]["state"] == "absent"


def test_needs_audio_never_says_no_when_a_window_is_ready():
    """needs_audio gates the decode, so it may be generous but never stricter than plan_window."""
    import random

    from earshotd.windowing import needs_audio, plan_window

    rng = random.Random(7)
    ready = 0
    for _ in range(40):
        pages, ts = [], T0
        for _stretch in range(rng.randint(1, 3)):
            for _ in range(rng.randint(5, 520)):
                quiet = np.array([rng.random() < 0.2 for _ in range(FRAMES)])
                pages.append(PageInfo(ts, FRAMES, quiet))
                ts += FRAMES * windowing.FRAME_MS + rng.choice(
                    [0, 0, 0, 800, 9_000, 40_000]
                )
            ts += rng.choice([0, 30_000, 119_000, 125_000, 400_000])
        end = pages[-1].end_ms
        for now in range(T0, end + 400_000, 7_000):
            capped = rng.random() < 0.5
            plan = plan_window(pages, now, capped=capped)
            if plan is not None:
                ready += 1
                assert needs_audio(pages, now, capped=capped), (now - T0, plan, capped)
                assert plan.reason in {"length", "gap", "idle", "cap"}
    assert ready > 100


def test_the_reported_reason_is_the_one_the_decision_used():
    from earshotd.windowing import plan_window

    assert plan_window(stretch(T0, 300), T0 + 300_000 + GAP_MS + 1_000).reason == "idle"
    first = stretch(T0, 200)
    later = stretch(first[-1].end_ms + GAP_MS + 5_000, 60)
    assert plan_window(first + later, later[-1].end_ms).reason == "gap"
    assert plan_window(stretch(T0, 720), T0 + 720_000 + SETTLE_MS).reason == "length"


# -- the bound on a load (unbounded-loads) --------------------------------------------------

BOUND_FRAMES = 66_000  # pipeline.MAX_LOAD_FRAMES: twice the frames of a LOAD_MS range
BOUND_BYTES = 13_332_000  # pipeline.MAX_LOAD_BYTES: BOUND_FRAMES * (2 + 200)


def flooded_pages(copies: int = 28) -> list:
    """The real fixture's pages stored `copies` times, each copy under its own session."""
    src = pages_mod.parse(FIXTURE.read_bytes())
    return [
        pages_mod.Page(
            p.session + 1_000_000 * k,
            p.page_index,
            p.page_ts_ms,
            p.frames,
            p.frame_count,
        )
        for k in range(copies)
        for p in src
    ]


class SpyDecode:
    """Records the frames and frame bytes of each pipeline._decode call, then decodes."""

    def __init__(self, monkeypatch, decode=None) -> None:
        from earshotd import pipeline as pipeline_mod

        self.frames: list[int] = []
        self.bytes: list[int] = []
        self.real = decode or pipeline_mod._decode
        monkeypatch.setattr(pipeline_mod, "_decode", self)

    def __call__(self, pages):
        self.frames.append(sum(len(p.frames) for p in pages))
        self.bytes.append(sum(len(f) + 2 for p in pages for f in p.frames))
        return self.real(pages)


def drain_at(pipeline, now: int) -> list:
    out = []
    while (r := pipeline.run_once(now)) is not None:
        out.append(r)
    return out


def untranscribed(config: Config) -> int:
    import sqlite3

    conn = sqlite3.connect(config.db_path)
    try:
        return conn.execute(
            "SELECT count(*) FROM pages WHERE transcribed_at IS NULL"
        ).fetchone()[0]
    finally:
        conn.close()


def test_a_flood_in_one_range_is_loaded_in_bounded_windows(tmp_path, monkeypatch):
    config = Config(tokens={}, data_dir=tmp_path)
    store.init(config.db_path)
    pages = flooded_pages(28)
    assert len(pages) == 1_008 and sum(p.frame_count for p in pages) == 67_200
    store.store_pages(config.db_path, pages)
    spy = SpyDecode(monkeypatch)
    models = FakeModels()
    last_end = max(p.page_ts_ms + p.frame_count * 20 for p in pages)
    done = drain_at(Pipeline(config, models), last_end + 600_000)
    assert [r.reason for r in done] == ["cap", "idle"]
    assert max(spy.frames) <= BOUND_FRAMES
    assert max(models.windows) <= BOUND_FRAMES * 320
    keys = [k for r in done for k in r.keys]
    assert len(keys) == len(set(keys)) == 1_008
    assert untranscribed(config) == 0


def test_a_flood_of_large_frames_is_bounded_by_bytes(tmp_path, monkeypatch):
    import struct

    config = Config(tokens={}, data_dir=tmp_path)
    store.init(config.db_path)
    # 1,000-byte frames (a valid opus TOC byte, then zeros): the real decoder takes them.
    frame = struct.pack("<H", 1_000) + b"\xb8" + bytes(999)
    pages = [pages_mod.Page(k, 0, T0, frame * 650, 650) for k in range(25)]
    assert len(pages[0].frames) == 651_300
    store.store_pages(config.db_path, pages)
    spy = SpyDecode(monkeypatch)
    done = drain_at(Pipeline(config, FakeModels()), T0 + 650 * 20 + 600_000)
    assert done[0].reason == "cap" and len(done[0].keys) == 20
    assert spy.bytes[0] == 13_026_000
    assert all(b <= BOUND_BYTES for b in spy.bytes), spy.bytes
    keys = [k for r in done for k in r.keys]
    assert len(keys) == len(set(keys)) == 25
    assert untranscribed(config) == 0


class RecordingModels(FakeModels):
    def __init__(self) -> None:
        super().__init__()
        self.audio: list[bytes] = []

    def transcribe(self, audio):
        self.audio.append(audio.tobytes())
        return super().transcribe(audio)


def drain_clock(config: Config) -> tuple[list, RecordingModels]:
    models = RecordingModels()
    pipeline = Pipeline(config, models)
    done, now = [], T0
    while now < T0 + 50 * 60_000:
        done += drain_at(pipeline, now)
        now += 30_000
    return done, models


def copies_store(tmp_path, name: str, extra: int):
    """The 35 minute stretch and 3 minute tail, plus `extra` copies of the first 10 minutes."""
    config = Config(tokens={}, data_dir=tmp_path / name)
    store.init(config.db_path)
    pages = shifted_fixture(0, 35 * 60_000, T0) + shifted_fixture(
        40 * 60_000, 3 * 60_000, T0
    )
    for k in range(extra):
        pages += [
            pages_mod.Page(
                p.session + 1_000_000 * (k + 1),
                p.page_index,
                p.page_ts_ms,
                p.frames,
                p.frame_count,
            )
            for p in shifted_fixture(0, 10 * 60_000, T0)
        ]
    store.store_pages(config.db_path, pages)
    return config, pages


def range_frames(pages: list) -> int:
    first = min(p.page_ts_ms for p in pages)
    return sum(
        p.frame_count for p in pages if p.page_ts_ms <= first + windowing.LOAD_MS
    )


@pytest.mark.parametrize("extra", [0, 4])
def test_a_load_under_the_bound_gives_the_windows_an_unbounded_load_gave(
    tmp_path, monkeypatch, extra
):
    from earshotd import pipeline as pipeline_mod

    bounded_config, pages = copies_store(tmp_path, "bounded", extra)
    if extra:
        assert 33_000 < range_frames(pages) <= BOUND_FRAMES, range_frames(pages)
    bounded, bounded_models = drain_clock(bounded_config)
    monkeypatch.setattr(pipeline_mod, "MAX_LOAD_FRAMES", sys.maxsize)
    monkeypatch.setattr(pipeline_mod, "MAX_LOAD_BYTES", sys.maxsize)
    free_config, _ = copies_store(tmp_path, "free", extra)
    free, free_models = drain_clock(free_config)
    assert len(bounded) == len(free) >= 5
    for a, b in zip(bounded, free, strict=True):
        assert (a.keys, a.reason, a.first_ms, a.last_ms) == (
            b.keys,
            b.reason,
            b.first_ms,
            b.last_ms,
        )
        assert a.reason != "cap"
    assert bounded_models.audio == free_models.audio


# -- the cap close ---------------------------------------------------------------------------


def test_a_capped_load_closes_as_cap_once_its_last_page_has_settled():
    from earshotd.windowing import Plan, needs_audio, plan_window

    pages = stretch(T0, 300)
    now = pages[-1].end_ms + SETTLE_MS
    assert plan_window(pages, now) is None
    assert plan_window(pages, now, capped=True) == Plan(len(pages), "cap")
    assert needs_audio(pages, now, capped=True)
    assert not needs_audio(pages, now)
    assert plan_window(pages, now - 1, capped=True) is None
    assert not needs_audio(pages, now - 1, capped=True)


def test_a_capped_load_still_closes_at_a_gap_or_a_full_window():
    from earshotd.windowing import plan_window

    first = stretch(T0, 300)
    later = stretch(first[-1].end_ms + GAP_MS + 1, 60)
    plan = plan_window(first + later, later[-1].end_ms, capped=True)
    assert plan is not None and (plan.count, plan.reason) == (len(first), "gap")
    long = stretch(T0, 720)
    now = T0 + 720_000 + SETTLE_MS
    free = plan_window(long, now)
    assert free is not None and free.reason == "length"
    assert plan_window(long, now, capped=True) == free
