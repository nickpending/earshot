import hashlib
import sqlite3
import tracemalloc

import pytest
from conftest import up
from test_records import raw
from test_upload_v2 import CT2, ORDER, rows, v2_body

from earshotd import cli, records, store
from earshotd.config import Config

_RECORDS = "SELECT * FROM pendant_records ORDER BY 1, 2, 3, 4"


def test_redecode_rebuilds_records_and_pages(client, config):
    client.post("/v1/pages", content=v2_body(*ORDER), headers=up(ctype=CT2))
    before_records = rows(config, _RECORDS)
    before_pages = rows(config, "SELECT * FROM pages ORDER BY page_index")
    assert before_records and before_pages
    conn = sqlite3.connect(config.db_path)
    conn.execute("DELETE FROM pendant_records")
    conn.execute("DELETE FROM pages")
    conn.commit()
    conn.close()
    assert cli.main(["redecode"], config=config) == 0
    assert rows(config, _RECORDS) == before_records
    assert rows(config, "SELECT * FROM pages ORDER BY page_index") == before_pages


def test_redecode_leaves_transcriptions_alone(client, config):
    client.post("/v1/pages", content=v2_body(*ORDER), headers=up(ctype=CT2))
    conn = store.connect(config.db_path)
    conn.execute("UPDATE pages SET transcribed_at = 123")
    conn.execute(
        "INSERT INTO segments (start_ms, end_ms, text, source)"
        " VALUES (1, 2, 'hi', 'pendant')"
    )
    conn.close()
    assert cli.main(["redecode"], config=config) == 0
    assert rows(config, "SELECT DISTINCT transcribed_at FROM pages") == [(123,)]
    assert rows(config, "SELECT text FROM segments") == [("hi",)]


def test_redecode_reports_an_undecodable_raw_page(client, config, capsys):
    client.post("/v1/pages", content=v2_body(3936), headers=up(ctype=CT2))
    conn = store.connect(config.db_path)
    conn.execute("INSERT INTO raw_pages VALUES (1, 2, 'x', X'1001')")
    conn.close()
    assert cli.main(["redecode"], config=config) == 1
    assert "page 2" in capsys.readouterr().err


# -- bounded and streamed (unbounded-loads) ---------------------------------------------------


def raw_store(tmp_path, name, rows):
    """A store whose raw_pages hold `rows`: (session, page_index, sha256, raw)."""
    path = tmp_path / name / "earshot.db"
    store.init(path)
    conn = store.connect(path)
    try:
        conn.executemany("INSERT INTO raw_pages VALUES (?, ?, ?, ?)", rows)
    finally:
        conn.close()
    return path


def traced_peak(path):
    tracemalloc.start()
    try:
        result = store.redecode(path)
        return result, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_redecode_memory_does_not_grow_with_the_raw_pages(tmp_path):
    buf = raw(66064)
    assert len(buf) == 4004

    def many(n):
        return [(i, i, f"{i:064x}", buf) for i in range(n)]

    small, small_peak = traced_peak(raw_store(tmp_path, "small", many(2000)))
    large, large_peak = traced_peak(raw_store(tmp_path, "large", many(8000)))
    assert small == (2000, []) and large == (8000, [])
    assert large_peak - small_peak < 1024 * 1024, (small_peak, large_peak)


def test_redecode_is_one_transaction_that_a_failure_rolls_back(tmp_path, monkeypatch):
    rows_ = [
        (records.decode(raw(n)).session, n, hashlib.sha256(raw(n)).hexdigest(), raw(n))
        for n in ORDER
    ]
    path = raw_store(tmp_path, "atomic", rows_)
    assert store.redecode(path) == (6, [])

    def snapshot():
        conn = store.connect(path)
        try:
            return (
                conn.execute(_RECORDS).fetchall(),
                conn.execute("SELECT * FROM pages ORDER BY 1, 2, 3").fetchall(),
            )
        finally:
            conn.close()

    before = snapshot()
    assert before[0] and before[1]
    real, calls = records.decode, []

    def decode(data):
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("third page")
        return real(data)

    monkeypatch.setattr(store.records, "decode", decode)
    with pytest.raises(RuntimeError, match="third page"):
        store.redecode(path)
    assert snapshot() == before


def failing_rows(n):
    return [(1, i, "x", b"\x10\x01") for i in range(n)]


def fixture_row():
    return (
        records.decode(raw(3936)).session,
        3936,
        hashlib.sha256(raw(3936)).hexdigest(),
        raw(3936),
    )


def expected_messages(n):
    out = []
    for _, i, _, data in failing_rows(n):
        try:
            records.decode(data)
        except records.DecodeError as e:
            out.append(f"session 1 page {i}: {e}")
    return out


@pytest.mark.parametrize(
    "failures, tail",
    [(250, "150 more raw pages"), (101, "1 more raw page"), (100, None)],
)
def test_redecode_names_at_most_100_failures_and_counts_the_rest(
    tmp_path, failures, tail
):
    path = raw_store(tmp_path, "named", [fixture_row(), *failing_rows(failures)])
    count, failed = store.redecode(path)
    want = expected_messages(failures)[:100] + ([tail] if tail else [])
    assert count == 1
    assert failed == want


def test_redecode_exits_1_with_101_lines_when_250_fail(tmp_path, capsys):
    path = raw_store(tmp_path, "cli", [fixture_row(), *failing_rows(250)])
    config = Config(tokens={}, data_dir=path.parent)
    assert cli.main(["redecode"], config=config) == 1
    assert len(capsys.readouterr().err.splitlines()) == 101


def test_redecode_refuses_while_a_worker_runs_and_works_after(client, config, capsys):
    from test_rebuild import FakeModels

    from earshotd import worker

    client.post("/v1/pages", content=v2_body(*ORDER), headers=up(ctype=CT2))
    before = rows(config, _RECORDS)
    conn = sqlite3.connect(config.db_path)
    conn.execute("DELETE FROM pendant_records")
    conn.commit()
    conn.close()
    running = worker.start(config, FakeModels())
    try:
        capsys.readouterr()
        assert cli.main(["redecode"], config=config) != 0
        assert "stop earshotd" in capsys.readouterr().err
        assert rows(config, _RECORDS) == []
    finally:
        running.stop.set()
        running.thread.join(timeout=30)
    assert not running.thread.is_alive()
    assert cli.main(["redecode"], config=config) == 0
    assert rows(config, _RECORDS) == before
