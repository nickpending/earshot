"""L5, L7: imported Limitless audio goes through the pipeline as source 'limitless', after pendant.

The models are stubs (the real ones are covered by test_pipeline_fixture); the audio is real Ogg
Opus written by the importer from the fake, and the windowing, linking and commit code is real.
"""

import sqlite3
import threading
from itertools import pairwise
from pathlib import Path

import numpy as np
import pytest
from conftest import RD
from fake_limitless import KEY, MEETING_MS, MEETING_TITLE, lifelog, standard_archive
from fastapi.testclient import TestClient

from earshotd import importer, limitless, pages, store, worker
from earshotd.app import create_app
from earshotd.diarize import Diarization, Turn
from earshotd.pipeline import Pipeline
from earshotd.transcribe import Segment
from earshotd.windowing import GAP_MS

FIXTURE_BODY = Path(__file__).resolve().parents[2] / "fixtures" / "say-test.pages.v1"
PENDANT_FIRST, PENDANT_LAST = 1790896082170, 1790896275991
PENDANT_READY = (
    PENDANT_LAST + 32 * 20 + GAP_MS + 1_000
)  # the fixture's window can close
PENDANT_YOUNG = PENDANT_LAST + 60_000
NEVER = 2**62  # a clock far enough on that nothing is "still arriving"


class Stub:
    name = "stub"

    def __init__(self) -> None:
        self.audio_ms: list[int] = []

    def transcribe(self, audio):
        self.audio_ms.append(len(audio) * 1000 // 16000)
        return [Segment(0, 1000, "words")] if len(audio) else []

    def diarize(self, audio):
        t = [Turn(0, 1000, "A")]
        return Diarization(t, t, {"A": np.ones(4, dtype=np.float32)})


def do_import(config, fake):
    store.init(config.db_path)
    report = importer.run(
        config,
        limitless.Client(KEY, fake.base_url, sleep=lambda _s: None),
        out=lambda *_a: None,
    )
    assert report.ok
    return report


def rows(config, sql, args=()):
    conn = sqlite3.connect(config.db_path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def test_l7_a_ready_pendant_window_goes_before_imported_audio(
    limitless_config, make_fake
):
    fake = make_fake([lifelog(0, MEETING_MS, MEETING_MS + 20_000)], audio_seconds=20)
    do_import(limitless_config, fake)
    store.store_pages(limitless_config.db_path, pages.parse(FIXTURE_BODY.read_bytes()))
    pipeline = Pipeline(limitless_config, Stub())

    first = pipeline.run_once(PENDANT_READY)
    assert first is not None and first.source == "pendant"
    assert rows(
        limitless_config,
        "SELECT count(*) FROM limitless_audio WHERE transcribed_at IS NULL",
    ) == [(1,)]
    second = pipeline.run_once(PENDANT_READY)
    assert second is not None and second.source == "limitless"
    assert pipeline.run_once(PENDANT_READY) is None
    assert {r[0] for r in rows(limitless_config, "SELECT source FROM segments")} == {
        "pendant",
        "limitless",
    }


def test_l7_imported_audio_waits_while_a_pendant_window_is_ready_every_time(
    limitless_config, make_fake
):
    """Even with several chunks queued, each pick re-checks pendant first."""
    fake = make_fake(
        [
            lifelog(0, MEETING_MS, MEETING_MS + 20_000),
            lifelog(1, MEETING_MS + 60_000, MEETING_MS + 80_000),
        ],
        audio_seconds=20,
    )
    do_import(limitless_config, fake)
    pipeline = Pipeline(limitless_config, Stub())
    assert pipeline.run_once(NEVER).source == "limitless"  # no pendant pages yet
    store.store_pages(limitless_config.db_path, pages.parse(FIXTURE_BODY.read_bytes()))
    nxt = pipeline.run_once(NEVER)
    assert nxt is not None and nxt.source == "pendant"  # arrived meanwhile: goes first
    assert pipeline.run_once(NEVER).source == "limitless"
    assert pipeline.run_once(NEVER) is None


def test_l7_imported_audio_is_taken_when_the_pendant_window_is_not_ready(
    limitless_config, make_fake
):
    fake = make_fake([lifelog(0, MEETING_MS, MEETING_MS + 20_000)], audio_seconds=20)
    do_import(limitless_config, fake)
    store.store_pages(limitless_config.db_path, pages.parse(FIXTURE_BODY.read_bytes()))
    pipeline = Pipeline(limitless_config, Stub())
    taken = pipeline.run_once(PENDANT_YOUNG)  # the pendant cut is still too young
    assert taken is not None and taken.source == "limitless"
    assert pipeline.run_once(PENDANT_YOUNG) is None
    assert rows(
        limitless_config, "SELECT count(*) FROM pages WHERE transcribed_at IS NULL"
    ) == [(36,)]


def test_a_long_chunk_is_windowed_resumed_and_marked_done_only_at_its_end(
    limitless_config, make_fake
):
    span = 25 * 60_000
    fake = make_fake(
        [lifelog(0, MEETING_MS, MEETING_MS + span)], audio_seconds=span / 1000
    )
    do_import(limitless_config, fake)
    stub = Stub()
    results = []
    while True:
        r = Pipeline(limitless_config, stub).run_once(
            NEVER
        )  # a fresh pipeline each time: a restart
        if r is None:
            break
        results.append(r)
        done, finished = rows(
            limitless_config, "SELECT done_ms, transcribed_at FROM limitless_audio"
        )[0]
        assert done == r.last_ms
        assert (finished is not None) == (
            len(results) > 2 and r.last_ms >= MEETING_MS + span - 40
        )
    assert len(results) >= 3
    assert results[0].first_ms == MEETING_MS
    assert all(
        b.first_ms == a.last_ms for a, b in pairwise(results)
    )  # no gap, no overlap
    assert all(r.source == "limitless" for r in results)
    assert results[-1].last_ms >= MEETING_MS + span - 40
    assert abs(sum(stub.audio_ms) - span) <= 60
    assert max(stub.audio_ms) <= 11 * 60_000
    assert rows(
        limitless_config,
        "SELECT count(*) FROM limitless_audio WHERE transcribed_at IS NULL",
    ) == [(0,)]
    segs = rows(limitless_config, "SELECT start_ms FROM segments ORDER BY start_ms")
    assert [s[0] for s in segs] == [r.first_ms for r in results]


def test_chunks_are_taken_in_time_order(limitless_config, make_fake):
    fake = make_fake(
        [
            lifelog(0, MEETING_MS + 3_600_000, MEETING_MS + 3_620_000),
            lifelog(1, MEETING_MS, MEETING_MS + 20_000),
        ],
        audio_seconds=20,
    )
    do_import(limitless_config, fake)
    pipeline = Pipeline(limitless_config, Stub())
    a, b = pipeline.run_once(NEVER), pipeline.run_once(NEVER)
    assert a is not None and b is not None
    assert (a.first_ms, b.first_ms) == (MEETING_MS, MEETING_MS + 3_600_000)


def test_a_window_that_fails_leaves_the_chunk_untranscribed(
    limitless_config, make_fake
):
    fake = make_fake([lifelog(0, MEETING_MS, MEETING_MS + 20_000)], audio_seconds=20)
    do_import(limitless_config, fake)

    class Broken(Stub):
        def diarize(self, audio):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        Pipeline(limitless_config, Broken()).run_once(NEVER)
    assert rows(
        limitless_config, "SELECT done_ms, transcribed_at FROM limitless_audio"
    ) == [(None, None)]
    assert rows(limitless_config, "SELECT count(*) FROM segments") == [(0,)]


def test_drain_stops_between_windows_when_asked(limitless_config, make_fake):
    fake = make_fake(
        [
            lifelog(i, MEETING_MS + i * 60_000, MEETING_MS + i * 60_000 + 20_000)
            for i in range(4)
        ],
        audio_seconds=20,
    )
    do_import(limitless_config, fake)
    stop = threading.Event()
    stub = Stub()

    class Stopping(Pipeline):
        def run_once(self, now_ms):
            result = super().run_once(now_ms)
            stop.set()  # asked to stop while the first window was being worked
            return result

    done = worker.drain(Stopping(limitless_config, stub), lambda: NEVER, stop)
    assert done == 1
    assert rows(
        limitless_config,
        "SELECT count(*) FROM limitless_audio WHERE transcribed_at IS NULL",
    ) == [(3,)]


def test_l5_the_read_api_serves_the_imported_archive(limitless_config, make_fake):
    fake = make_fake(standard_archive(extra=4), audio_seconds=6)
    do_import(limitless_config, fake)
    done = worker.drain(Pipeline(limitless_config, Stub()), lambda: NEVER)
    assert done == 8  # one window per 6 second chunk: 1 + 3 + 4
    client = TestClient(create_app(limitless_config))
    auth = {"Authorization": f"Bearer {RD}"}

    days = {d["date"]: d for d in client.get("/v1/days", headers=auth).json()}
    assert days["2025-11-04"]["sources"] == ["limitless"]

    transcript = client.get("/v1/days/2025-11-04/transcript", headers=auth).json()
    segs = transcript["segments"]
    assert segs and {s["source"] for s in segs} == {"limitless"}
    meeting = [
        s for s in segs if MEETING_MS <= s["start_ms"] < MEETING_MS + 45 * 60_000
    ]
    assert meeting, "no segment covers the 19:00:23Z team meeting"
    assert all(s["speaker"] and s["speaker"].startswith("S") for s in segs)
    assert len({s["speaker"] for s in segs}) == 1  # one voice, one day-level label

    originals = client.get("/v1/days/2025-11-04/limitless", headers=auth).json()
    team = [o for o in originals if o["lifelog_id"] == "ll00000"]
    assert team and team[0]["title"] == MEETING_TITLE
    assert team[0]["markdown"].startswith("# Team meeting")
    assert team[0]["starred"] is True
