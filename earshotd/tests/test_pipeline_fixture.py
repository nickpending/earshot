"""C4: the fixture's pages, once old enough, become pendant segments in the database.

Real PyAV, faster-whisper and pyannote run; only the clock is injected.
"""

import os
import re
import sqlite3
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import pytest
from conftest import RD, UP, up
from fastapi.testclient import TestClient
from faster_whisper.utils import download_model
from model_paths import SNAPSHOT

from earshotd import pages as pages_mod
from earshotd import store
from earshotd.app import create_app
from earshotd.config import Config
from earshotd.pipeline import LocalModels, Pipeline
from earshotd.windowing import GAP_MS

FIXTURE_BODY = Path(__file__).resolve().parents[2] / "fixtures" / "say-test.pages.v1"
FIRST_TS, LAST_TS = 1790896082170, 1790896275991
LAST_DURATION_MS = 32 * 20  # the last page carries 32 frames
WORDS = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]


@pytest.fixture(scope="module")
def ran(tmp_path_factory):
    data = tmp_path_factory.mktemp("earshotd")
    config = Config(
        tokens={UP: "upload", RD: "read"},
        data_dir=data,
        whisper_model=download_model("large-v3-turbo", local_files_only=True),
        diarization_model=str(SNAPSHOT),
    )
    client = TestClient(create_app(config))
    body = (
        Path(__file__).resolve().parents[2] / "fixtures" / "say-test.pages.v1"
    ).read_bytes()
    assert client.post("/v1/pages", content=body, headers=up()).status_code == 200
    pipeline = Pipeline(config, LocalModels(config))
    young = LAST_TS + 60_000
    assert pipeline.run_once(young) is None  # not yet old enough to close a window
    result = pipeline.run_once(LAST_TS + LAST_DURATION_MS + GAP_MS + 1_000)
    assert result is not None
    return config, client, result


def rows(config: Config, sql: str) -> list[tuple]:
    conn = sqlite3.connect(config.db_path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


@pytest.mark.model("diarization", "whisper")
def test_segments_are_pendant_text_inside_the_fixture_time_range(ran):
    config, _client, _result = ran
    segs = rows(config, "SELECT start_ms, end_ms, text, source, speaker FROM segments")
    assert segs
    text = " ".join(s[2] for s in segs).lower()
    assert "quick brown fox" in text
    pos = 0
    for n, word in enumerate(WORDS, start=1):
        m = re.search(rf"\b({n}|{word})\b", text[pos:])
        assert m, f"{word} not found in order in {text!r}"
        pos += m.end()
    for start, end, _t, source, speaker in segs:
        assert source == "pendant"
        assert FIRST_TS <= start <= end <= LAST_TS + LAST_DURATION_MS
        assert re.fullmatch(r"S\d+", speaker or "")


@pytest.mark.model("diarization", "whisper")
def test_every_page_is_marked_transcribed_with_the_segments(ran):
    config, _client, result = ran
    assert rows(config, "SELECT count(*) FROM pages WHERE transcribed_at IS NULL") == [
        (0,)
    ]
    assert len(result.keys) == 36
    assert len(rows(config, "SELECT 1 FROM day_speakers")) >= 1


@pytest.mark.model("diarization", "whisper")
def test_no_transcript_files_outside_the_database(ran):
    config, _client, _result = ran
    names = {p.name for p in Path(config.data_dir).iterdir()}
    assert names <= {"earshot.db", "earshot.db-wal", "earshot.db-shm"}, names


@pytest.mark.model("diarization", "whisper")
def test_read_api_serves_the_pipeline_output(ran):
    _config, client, _result = ran
    auth = {"Authorization": f"Bearer {RD}"}
    days = client.get("/v1/days", headers=auth).json()
    assert [d["date"] for d in days] == ["2026-10-01"]
    assert days[0]["sources"] == ["pendant"]
    md = client.get(
        "/v1/days/2026-10-01/transcript", headers={**auth, "Accept": "text/markdown"}
    )
    assert md.status_code == 200 and "quick brown fox" in md.text.lower()


def test_commit_is_one_transaction(tmp_path):
    """If marking pages fails, no segment of the window survives."""
    config = Config(tokens={}, data_dir=tmp_path)
    store.init(config.db_path)
    body = (
        Path(__file__).resolve().parents[2] / "fixtures" / "say-test.pages.v1"
    ).read_bytes()
    parsed = pages_mod.parse(body)
    store.store_pages(config.db_path, parsed)

    class Models:
        name = "fake"

        def transcribe(self, audio):
            from earshotd.transcribe import Segment

            return [Segment(0, 1000, "hello")]

        def diarize(self, audio):
            import numpy as np

            from earshotd.diarize import Diarization, Turn

            t = [Turn(0, 1000, "A")]
            return Diarization(t, t, {"A": np.ones(4, dtype=np.float32)})

    conn = sqlite3.connect(config.db_path)
    conn.execute(
        "UPDATE pages SET transcribed_at = 1 WHERE page_index = 105"
    )  # raced elsewhere
    conn.commit()
    conn.close()
    pipeline = Pipeline(config, Models())
    window = pipeline_pages(config)
    with pytest.raises(RuntimeError):
        pipeline.process_window(window)
    assert rows(config, "SELECT count(*) FROM segments") == [(0,)]
    assert rows(config, "SELECT count(*) FROM day_speakers") == [(0,)]
    assert rows(config, "SELECT count(*) FROM pages WHERE transcribed_at IS NULL") == [
        (35,)
    ]


def pipeline_pages(config):
    from earshotd.pipeline import StoredPage
    from earshotd.windowing import split_frames

    conn = sqlite3.connect(config.db_path)
    try:
        return [
            StoredPage(s, i, t, split_frames(f))
            for s, i, t, f in conn.execute(
                "SELECT session, page_index, page_ts_ms, frames FROM pages ORDER BY page_ts_ms"
            )
        ]
    finally:
        conn.close()


class EnrollModels:
    name = "fake"

    def diarize(self, audio):
        import numpy as np

        from earshotd.diarize import Diarization, Turn

        t = [Turn(0, 1000, "A")]
        return Diarization(t, t, {"A": np.ones(4, dtype=np.float32)})


def test_enroll_refuses_a_bad_name_and_an_overwrite_unless_asked(tmp_path):
    from earshotd.pipeline import EnrollError, enroll

    config = Config(tokens={}, data_dir=tmp_path)
    store.init(config.db_path)
    store.store_pages(config.db_path, pages_mod.parse(FIXTURE_BODY.read_bytes()))
    args = (EnrollModels(),)
    span = (FIRST_TS, FIRST_TS + 10_000)
    with pytest.raises(EnrollError):
        enroll(config, *args, "  ", *span)
    enroll(config, *args, "FEE013", *span)
    before = rows(config, "SELECT embedding FROM voices")
    with pytest.raises(EnrollError, match="--replace"):
        enroll(config, *args, "FEE013", *span)
    assert rows(config, "SELECT embedding FROM voices") == before
    enroll(config, *args, "FEE013", *span, replace=True)
    assert rows(config, "SELECT count(*) FROM voices") == [(1,)]
