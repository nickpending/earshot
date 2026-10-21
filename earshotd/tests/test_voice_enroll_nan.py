"""`earshot voice add` refuses a range whose dominant speaker has no usable fingerprint."""

import sqlite3

import numpy as np
import pytest
from test_pipeline_fixture import FIRST_TS, FIXTURE_BODY
from test_windowing import flooded_pages

from earshotd import cli, pipeline, store
from earshotd import pages as pages_mod
from earshotd.config import Config
from earshotd.diarize import Diarization, Turn


class NoFingerprintModels:
    """A speaks most but has no fingerprint (diarize.diarize drops NaN and zero ones)."""

    name = "fake"

    def diarize(self, audio):
        turns = [Turn(0, 3000, "A"), Turn(3000, 4000, "B")]
        return Diarization(turns, turns, {"B": np.ones(4, dtype=np.float32)})


def test_voice_add_refuses_when_the_dominant_speaker_has_no_usable_voice(
    tmp_path, capsys
):
    config = Config(tokens={}, data_dir=tmp_path)
    store.init(config.db_path)
    store.store_pages(config.db_path, pages_mod.parse(FIXTURE_BODY.read_bytes()))
    code = cli.main(
        [
            "voice", "add", "Ada",
            "--from", str(FIRST_TS),
            "--to", str(FIRST_TS + 10_000),
        ],
        config=config,
        models=NoFingerprintModels(),
    )  # fmt: skip
    assert code != 0
    assert "no usable voice" in capsys.readouterr().err
    conn = sqlite3.connect(config.db_path)
    try:
        assert conn.execute("SELECT count(*) FROM voices").fetchone() == (0,)
    finally:
        conn.close()


class RecordingDiarize:
    name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def diarize(self, audio):
        self.calls += 1
        turns = [Turn(0, 3000, "A")]
        return Diarization(turns, turns, {"A": np.ones(4, dtype=np.float32)})


def voices(config) -> list[tuple]:
    conn = sqlite3.connect(config.db_path)
    try:
        return conn.execute("SELECT name FROM voices").fetchall()
    finally:
        conn.close()


def test_enroll_refuses_a_range_over_the_load_bound_before_diarizing(tmp_path):
    config = Config(tokens={}, data_dir=tmp_path)
    store.init(config.db_path)
    store.store_pages(config.db_path, flooded_pages(28))  # 67,200 frames in one range
    models = RecordingDiarize()
    with pytest.raises(pipeline.EnrollError, match="22 minutes"):
        pipeline.enroll(config, models, "Ada", FIRST_TS - 1, FIRST_TS + 10_000_000)
    assert models.calls == 0
    assert voices(config) == []


def test_enroll_takes_a_range_inside_the_load_bound(tmp_path):
    config = Config(tokens={}, data_dir=tmp_path)
    store.init(config.db_path)
    store.store_pages(config.db_path, flooded_pages(1))
    models = RecordingDiarize()
    pipeline.enroll(config, models, "Ada", FIRST_TS - 1, FIRST_TS + 10_000_000)
    assert models.calls == 1
    assert voices(config) == [("Ada",)]
