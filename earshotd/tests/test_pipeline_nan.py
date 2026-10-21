"""A speaker without a usable fingerprint is never linked, stored or named.

C2 runs the real community-1 model on 1.5 s of speech in 30 s of silence (a NaN fingerprint
today); C3 puts a NaN row and an all-zero row beside a normal one in a fake pyannote output.
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import sqlite3

import numpy as np
import pytest
from model_paths import SNAPSHOT
from test_diarization_nan import sparse_speech
from test_linking_ami import encode_opus, pages_of
from test_pipeline_fixture import FIXTURE_BODY, pipeline_pages

from earshotd import diarize, linking, store
from earshotd import pages as pages_mod
from earshotd.config import Config
from earshotd.pipeline import Pipeline
from earshotd.transcribe import Segment


def rows(config: Config, sql: str) -> list[tuple]:
    conn = sqlite3.connect(config.db_path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


class RealDiarizeModels:
    """Real pyannote; a transcript with one line on the speech and one on silence."""

    name = "fake"

    def __init__(self) -> None:
        self._diarizer = diarize.load_diarizer(SNAPSHOT)

    def transcribe(self, audio):
        return [Segment(10_000, 11_500, "speech"), Segment(20_000, 21_000, "later")]

    def diarize(self, audio):
        return diarize.diarize(self._diarizer, audio)


@pytest.mark.model("diarization")
def test_real_model_nan_fingerprint_stores_no_centroid_and_null_speakers(tmp_path):
    from earshotd.pipeline import StoredPage
    from earshotd.windowing import split_frames

    config = Config(tokens={}, data_dir=tmp_path)
    store.init(config.db_path)
    frames = encode_opus(sparse_speech())
    pages = pages_of(frames, 0, len(frames), 0)
    store.store_pages(config.db_path, pages)
    window = [
        StoredPage(p.session, p.page_index, p.page_ts_ms, split_frames(p.frames))
        for p in pages
    ]
    Pipeline(config, RealDiarizeModels()).process_window(window)

    for (blob,) in rows(config, "SELECT centroid FROM day_speakers"):
        vec = linking.unpack(blob)
        assert np.isfinite(vec).all() and np.linalg.norm(vec) > 0
    lines = rows(config, "SELECT text, speaker, name FROM segments ORDER BY id")
    assert [t for t, _s, _n in lines] == ["speech", "later"]
    assert all(speaker is None and name is None for _t, speaker, name in lines)
    assert rows(config, "SELECT count(*) FROM day_speakers") == [(0,)]


class _Labels:
    def __init__(self, labels):
        self._labels = labels

    def labels(self):
        return self._labels

    def itertracks(self, yield_label=True):
        return iter(())


class _PyannoteOut:
    """What pyannote returns: a normal row, a NaN row and an all-zero row."""

    speaker_diarization = _Labels(["A", "B", "C"])
    exclusive_speaker_diarization = _Labels(["A", "B", "C"])
    speaker_embeddings = np.array(
        [[1, 0, 0, 0], [np.nan] * 4, [0, 0, 0, 0]], dtype=np.float32
    )


class OddRowsModels:
    name = "fake"

    def transcribe(self, audio):
        return [
            Segment(0, 1000, "normal"),
            Segment(1000, 2000, "nan"),
            Segment(2000, 3000, "zero"),
        ]

    def diarize(self, audio):
        result = diarize.diarize(lambda _inputs: _PyannoteOut(), audio)
        turns = [
            diarize.Turn(0, 1000, "A"),
            diarize.Turn(1000, 2000, "B"),
            diarize.Turn(2000, 3000, "C"),
        ]
        return diarize.Diarization(turns, turns, result.embeddings)


def test_only_the_normal_speaker_is_linked_over_two_windows(tmp_path):
    config = Config(tokens={}, data_dir=tmp_path)
    store.init(config.db_path)
    store.store_pages(config.db_path, pages_mod.parse(FIXTURE_BODY.read_bytes()))
    pages = pipeline_pages(config)
    pipeline = Pipeline(config, OddRowsModels())
    pipeline.process_window(pages[:10])
    pipeline.process_window(pages[10:20])

    assert rows(config, "SELECT speaker FROM day_speakers") == [("S1",)]
    assert (
        rows(config, "SELECT text, speaker FROM segments ORDER BY id")
        == [
            ("normal", "S1"),
            ("nan", None),
            ("zero", None),
        ]
        * 2
    )
