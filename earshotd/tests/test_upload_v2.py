import sqlite3
import struct

from conftest import UP, up
from test_records import FIX, raw

from earshotd import records

CT2 = "application/vnd.earshot.pages.v2"
ORDER = (3936, 3937, 4891, 3935, 49052, 49123)


def v2_body(*pages: int) -> bytes:
    return b"".join(struct.pack("<I", len(raw(n))) + raw(n) for n in pages)


def rows(config, sql):
    conn = sqlite3.connect(config.db_path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def post(client, body, token=UP):
    return client.post("/v1/pages", content=body, headers=up(token, CT2))


def test_all_fixtures_stored_and_resend_is_duplicate(client, config):
    r = post(client, v2_body(*ORDER))
    assert r.status_code == 200
    assert r.json() == {"stored": 6, "duplicates": 0}
    stored = {
        (s, i): bytes(b)
        for s, i, b in rows(config, "SELECT session, page_index, raw FROM raw_pages")
    }
    assert stored == {(records.decode(raw(n)).session, n): raw(n) for n in ORDER}
    r = post(client, v2_body(*ORDER))
    assert r.json() == {"stored": 0, "duplicates": 6}
    assert rows(config, "SELECT count(*) FROM raw_pages") == [(6,)]


def test_audio_pages_match_what_v1_stores(client, config):
    post(client, v2_body(*ORDER))
    got = rows(
        config,
        "SELECT page_index, page_ts_ms, frames, transcribed_at FROM pages"
        " ORDER BY page_index",
    )
    assert [g[0] for g in got] == [3936, 3937, 49123]
    for idx, ts, frames, done in got:
        a = records.decode(raw(idx)).audio
        assert (ts, bytes(frames), done) == (a.page_ts_ms, a.frames, None)
        # the same page sent through v1 is a duplicate: identical key and frames
        v1 = struct.pack("<IIQH", a.session, idx, a.page_ts_ms, a.frame_count)
        r = client.post("/v1/pages", content=v1 + a.frames, headers=up())
        assert r.json() == {"stored": 0, "duplicates": 1}


def test_records_stored_with_kind_ts_and_json(client, config):
    post(client, v2_body(*ORDER))
    kinds = {k for (k,) in rows(config, "SELECT DISTINCT kind FROM pendant_records")}
    assert {"log", "battery", "motion", "thread_stats", "marker", "crc_failed"} <= kinds
    assert "unknown" in kinds
    assert rows(
        config, "SELECT ts_ms FROM pendant_records WHERE kind = 'crc_failed'"
    ) == [(None,)]
    (data,) = rows(
        config,
        "SELECT data FROM pendant_records WHERE kind = 'log' AND page_index = 3936"
        " ORDER BY ts_ms LIMIT 1",
    )
    assert '"module": "storage"' in data[0]


def test_truncated_body_is_400_and_stores_nothing(client, config):
    body = v2_body(3936, 3937)
    r = post(client, body[:-10])
    assert r.status_code == 400
    assert "byte" in r.json()["error"]
    r = post(client, body[:3])
    assert r.status_code == 400 and "byte 0" in r.json()["error"]
    assert rows(config, "SELECT count(*) FROM raw_pages") == [(0,)]
    assert rows(config, "SELECT count(*) FROM pages") == [(0,)]


def test_buffer_without_page_index_is_400_and_stores_nothing(client, config):
    bad = struct.pack("<I", 2) + bytes([0x10, 0x01])
    r = post(client, v2_body(3936) + bad)
    assert r.status_code == 400
    assert "page index" in r.json()["error"]
    assert rows(config, "SELECT count(*) FROM raw_pages") == [(0,)]
    assert rows(config, "SELECT count(*) FROM pages") == [(0,)]


def test_body_of_1_mib_plus_one_full_storage_buffer_is_accepted(client):
    # Spool.swift closes a segment at 1 MiB and the last record can be a 4049-byte StorageBuffer
    # (4049 is page 61433 as captured; the committed fixtures are smaller, so records are appended
    # until the body is as large as that segment).
    every = sorted(int(p.name.split(".")[0]) for p in FIX.glob("*.storagebuffer.hex"))
    records_in = 0
    chunks: list[bytes] = []
    size = 0
    while size < (1 << 20) + 4049:
        for n in every:
            chunks.append(v2_body(n))
            size += len(chunks[-1])
            records_in += 1
            if size >= (1 << 20) + 4049:
                break
    body = b"".join(chunks)
    assert len(body) >= (1 << 20) + 4049
    r = post(client, body)
    assert r.status_code == 200
    assert r.json()["stored"] + r.json()["duplicates"] == records_in


def test_upload_scope_required(client):
    assert post(client, b"", token=None).status_code == 401
    assert post(client, b"", token="b" * 64).status_code == 403
