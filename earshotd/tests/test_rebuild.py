"""`earshot rebuild` clears what is derived from audio, keeps the audio, and refuses while a worker runs."""

import numpy as np
from conftest import up
from test_upload_v2 import CT2, ORDER, rows, v2_body

from earshotd import cli, store, worker
from earshotd.diarize import Diarization, Turn
from earshotd.pipeline import Pipeline
from earshotd.transcribe import Segment

FAR = 2**50
KEPT = (
    "SELECT * FROM voices ORDER BY 1",
    "SELECT * FROM raw_pages ORDER BY 1, 2, 3",
    "SELECT session, page_index, page_ts_ms, frames FROM pages ORDER BY 1, 2, 3",
    "SELECT * FROM pendant_records ORDER BY 1, 2, 3, 4",
    "SELECT * FROM limitless_lifelogs ORDER BY 1",
    "SELECT lifelog_id, start_ms, end_ms, path FROM limitless_audio ORDER BY 1, 2, 3",
    "SELECT * FROM limitless_audio_absent ORDER BY 1, 2, 3",
)
DERIVED = (
    "SELECT count(*) FROM segments",
    "SELECT count(*) FROM day_speakers",
    "SELECT count(*) FROM pages WHERE transcribed_at IS NOT NULL",
    (
        "SELECT count(*) FROM limitless_audio"
        " WHERE transcribed_at IS NOT NULL OR done_ms IS NOT NULL"
    ),
)


class FakeModels:
    name = "fake"

    def transcribe(self, audio):
        return [Segment(0, 1000, "hello")]

    def diarize(self, audio):
        turns = [Turn(0, 1000, "A")]
        return Diarization(turns, turns, {"A": np.ones(4, dtype=np.float32)})


def populate(client, config) -> None:
    """Pendant pages transcribed, plus imported chunks done and half done, voices and lifelogs."""
    client.post("/v1/pages", content=v2_body(*ORDER), headers=up(ctype=CT2))
    assert worker.drain(Pipeline(config, FakeModels()), lambda: FAR) > 0
    conn = store.connect(config.db_path)
    try:
        conn.execute(
            "INSERT INTO voices VALUES ('Ada', X'0000803F0000803F0000803F0000803F')"
        )
        conn.execute(
            "INSERT INTO limitless_lifelogs VALUES ('l1', 't', 1, 2, 0, 'md', '{}')"
        )
        conn.execute(
            "INSERT INTO limitless_audio VALUES ('l1', 1, 2, '/data/a.ogg', 100, 2)"
        )
        conn.execute(
            "INSERT INTO limitless_audio VALUES ('l1', 3, 4, '/data/b.ogg', NULL, 3)"
        )
        conn.execute("INSERT INTO limitless_audio_absent VALUES ('l2', 5, 6, 'e', 7)")
    finally:
        conn.close()


def test_rebuild_clears_derived_rows_and_keeps_everything_else(client, config, capsys):
    populate(client, config)
    assert all(rows(config, q) != [(0,)] for q in DERIVED)
    kept = [rows(config, q) for q in KEPT]
    derived = [rows(config, q) for q in DERIVED]
    capsys.readouterr()

    assert cli.main(["rebuild"], config=config) == 0

    out = capsys.readouterr().out
    assert "segments" in out and "day_speakers" in out
    assert [rows(config, q) for q in DERIVED] == [[(0,)]] * len(DERIVED)
    assert [rows(config, q) for q in KEPT] == kept
    # The worker transcribes the same pages again.
    again = Pipeline(config, FakeModels())
    while again._run_pendant(
        FAR
    ):  # the imported chunks name files this test does not have
        pass
    assert [rows(config, q) for q in DERIVED[:3]] == derived[:3]


def test_rebuild_refuses_while_a_worker_runs_and_works_after(client, config, capsys):
    populate(client, config)
    before = [rows(config, q) for q in DERIVED + KEPT]
    running = worker.start(config, FakeModels())
    try:
        capsys.readouterr()
        assert cli.main(["rebuild"], config=config) != 0
        assert "worker" in capsys.readouterr().err
        assert [rows(config, q) for q in DERIVED + KEPT] == before
    finally:
        running.stop.set()
        running.thread.join(timeout=30)
    assert not running.thread.is_alive()
    assert cli.main(["rebuild"], config=config) == 0
    assert rows(config, DERIVED[0]) == [(0,)]
