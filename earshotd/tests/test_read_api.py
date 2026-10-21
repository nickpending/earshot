import tracemalloc
from zoneinfo import ZoneInfo

import pytest
from conftest import RD, T0, UP, up
from fastapi.testclient import TestClient

from earshotd import queries, store
from earshotd.app import create_app
from earshotd.config import Config
from earshotd.read_api import MAX_SEGMENTS

DAY = "2026-10-01"
H = 3600_000
NEXT_DAY_START = T0 + 8 * H + 22 * 60_000  # 2026-10-02 00:30 PDT, still Oct 2 in UTC


@pytest.fixture
def config(tmp_path) -> Config:
    """Days are cut in Los Angeles here: T0 is 2026-10-01 16:08:02 there."""
    return Config(
        tokens={UP: "upload", RD: "read"},
        data_dir=tmp_path,
        zone=ZoneInfo("America/Los_Angeles"),
    )


def rd(token=RD):
    return {"Authorization": f"Bearer {token}"}


def seed(config):
    rows = [
        (T0, T0 + 4000, "The quick brown fox", "pendant", "S1", "Ada"),
        (T0 + 60_000, T0 + 70_000, "one two three", "pendant", "S2", None),
        (T0 + 600_000, T0 + 610_000, "counting", "pendant", "S1", "Ada"),
        (NEXT_DAY_START, NEXT_DAY_START + 60_000, "next day", "pendant", "S1", None),
    ]
    conn = store.connect(config.db_path)
    conn.executemany(
        "INSERT INTO segments (start_ms, end_ms, text, source, speaker, name)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.close()


def test_days_lists_counts_newest_first(client, config):
    seed(config)
    r = client.get("/v1/days", headers=rd())
    assert r.status_code == 200
    assert r.json() == [
        {
            "date": "2026-10-02",
            "segments": 1,
            "first_ms": NEXT_DAY_START,
            "last_ms": NEXT_DAY_START + 60_000,
            "sources": ["pendant"],
        },
        {
            "date": DAY,
            "segments": 3,
            "first_ms": T0,
            "last_ms": T0 + 610_000,
            "sources": ["pendant"],
        },
    ]


def test_days_range_filter(client, config):
    seed(config)
    r = client.get("/v1/days", params={"from": DAY, "to": DAY}, headers=rd())
    assert [d["date"] for d in r.json()] == [DAY]


def test_transcript_json(client, config):
    seed(config)
    r = client.get(f"/v1/days/{DAY}/transcript", headers=rd())
    assert r.status_code == 200
    body = r.json()
    assert body["date"] == DAY
    assert [s["text"] for s in body["segments"]] == [
        "The quick brown fox",
        "one two three",
        "counting",
    ]
    assert body["segments"][0] == {
        "start_ms": T0,
        "end_ms": T0 + 4000,
        "text": "The quick brown fox",
        "source": "pendant",
        "speaker": "S1",
        "name": "Ada",
    }
    assert body["segments"][1]["name"] is None


def test_transcript_markdown_in_los_angeles(client, config):
    seed(config)
    r = client.get(
        f"/v1/days/{DAY}/transcript", headers={**rd(), "Accept": "text/markdown"}
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert r.text.splitlines() == [
        "- 16:08:02 **Ada** The quick brown fox",
        "- 16:09:02 **S2** one two three",
        "- 16:18:02 **Ada** counting",
    ]


def test_transcript_range_only_counting(client, config):
    seed(config)
    r = client.get(
        "/v1/transcript",
        params={"from": T0 + 500_000, "to": T0 + 700_000},
        headers=rd(),
    )
    assert [s["text"] for s in r.json()["segments"]] == ["counting"]


def test_transcript_range_crosses_days(client, config):
    seed(config)
    r = client.get(
        "/v1/transcript", params={"from": T0, "to": T0 + 9 * H}, headers=rd()
    )
    assert len(r.json()["segments"]) == 4


def test_bad_date_is_400(client):
    assert client.get("/v1/days/nope/transcript", headers=rd()).status_code == 400


def test_limitless_empty_and_populated(client, config):
    assert client.get(f"/v1/days/{DAY}/limitless", headers=rd()).json() == []
    conn = store.connect(config.db_path)
    conn.execute(
        "INSERT INTO limitless_lifelogs VALUES ('L1','t',?,?,1,'md','{}')",
        (T0 - H, T0 - 3000_000),
    )
    conn.close()
    assert client.get(f"/v1/days/{DAY}/limitless", headers=rd()).json() == [
        {
            "lifelog_id": "L1",
            "title": "t",
            "start_ms": T0 - H,
            "end_ms": T0 - 3000_000,
            "starred": True,
            "markdown": "md",
        }
    ]


def test_scope_and_auth(client):
    paths = (
        "/v1/days",
        f"/v1/days/{DAY}/transcript",
        "/v1/transcript?from=0&to=1",
        f"/v1/days/{DAY}/limitless",
    )
    for path in paths:
        assert client.get(path).status_code == 401
        assert client.get(path, headers=rd("x" * 64)).status_code == 401
        assert client.get(path, headers=rd(UP)).status_code == 403


def test_health_is_liveness_only_without_a_token(client, body):
    client.post("/v1/pages", content=body, headers=up())
    r = client.get("/v1/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def traced_connect(monkeypatch, statements):
    real = store.connect

    def connect(db_path):
        conn = real(db_path)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(store, "connect", connect)


def test_health_is_500_on_a_store_without_its_schema(config):
    app = create_app(config)
    config.db_path.write_bytes(b"")
    for suffix in ("-wal", "-shm"):
        config.db_path.with_name(config.db_path.name + suffix).unlink(missing_ok=True)
    r = TestClient(app, raise_server_exceptions=False).get("/v1/health")
    assert r.status_code == 500


def test_health_runs_no_scan_and_reads_no_row(client, config, body, monkeypatch):
    client.post("/v1/pages", content=body, headers=up())
    statements: list[str] = []
    traced_connect(monkeypatch, statements)
    assert client.get("/v1/health").status_code == 200
    assert statements, "the route never ran a statement on the store"
    for sql in statements:
        low = sql.lower()
        assert "count(" not in low and "max(" not in low, sql
        if any(t in low for t in ("pages", "limitless_lifelogs", "limitless_audio")):
            assert low.rstrip("; ").endswith("limit 0"), sql


def test_status_carries_the_health_detail_for_a_read_token(client, body):
    client.post("/v1/pages", content=body, headers=up())
    assert client.get("/v1/status").status_code == 401
    assert client.get("/v1/status", headers=rd("x" * 64)).status_code == 401
    assert client.get("/v1/status", headers=rd(UP)).status_code == 403
    r = client.get("/v1/status", headers=rd())
    assert r.status_code == 200
    h = r.json()
    assert h["pages"] == 36
    assert h["untranscribed_pages"] == 36
    assert h["last_page_ts_ms"] == 1790896275991
    assert "import" in h
    assert h["worker"] == {"state": "absent", "failures": 0, "error": None}


def test_openapi_needs_a_read_token(client):
    assert client.get("/v1/openapi.json").status_code == 401
    assert client.get("/v1/openapi.json", headers=rd(UP)).status_code == 403
    r = client.get("/v1/openapi.json", headers=rd())
    assert r.status_code == 200
    assert set(r.json()["paths"]) == {
        "/v1/pages",
        "/v1/days",
        "/v1/days/{date}/transcript",
        "/v1/transcript",
        "/v1/days/{date}/limitless",
        "/v1/pendant/records",
        "/v1/health",
        "/v1/status",
    }


def test_every_route_but_health_answers_401_without_a_token(client):
    doc = client.get("/v1/openapi.json", headers=rd()).json()
    calls = [
        (method.upper(), path.replace("{date}", DAY))
        for path, ops in doc["paths"].items()
        for method in ops
    ]
    calls.append(("GET", "/v1/openapi.json"))
    assert ("GET", "/v1/health") in calls and len(calls) > 5
    for method, path in calls:
        want = 200 if (method, path) == ("GET", "/v1/health") else 401
        assert client.request(method, path).status_code == want, (method, path)


def test_inverted_ranges_are_400(client):
    r = client.get("/v1/transcript", params={"from": 10, "to": 5}, headers=rd())
    assert r.status_code == 400 and "error" in r.json()
    r = client.get("/v1/days", params={"from": "2026-10-02", "to": DAY}, headers=rd())
    assert r.status_code == 400 and "error" in r.json()


def test_non_integer_range_is_400_json_error(client):
    r = client.get("/v1/transcript", params={"from": "a", "to": 5}, headers=rd())
    assert r.status_code == 400 and "error" in r.json()
    r = client.get("/v1/transcript", params={"to": 5}, headers=rd())
    assert r.status_code == 400


def seed_many(config, n, step_ms=100):
    store.init(config.db_path)
    conn = store.connect(config.db_path)
    conn.executemany(
        "INSERT INTO segments (start_ms, end_ms, text, source, speaker, name)"
        " VALUES (?, ?, 'x', ?, 'S1', NULL)",
        (
            (
                T0 + i * step_ms,
                T0 + i * step_ms + 50,
                "pendant" if i % 2 else "limitless",
            )
            for i in range(n)
        ),
    )
    conn.close()


def segment_calls(client):
    span = {"from": T0 - 1, "to": T0 + 10_000_000}
    for markdown in (False, True):
        extra = {"Accept": "text/markdown"} if markdown else {}
        yield client.get("/v1/transcript", params=span, headers={**rd(), **extra})
        yield client.get(f"/v1/days/{DAY}/transcript", headers={**rd(), **extra})


def test_transcript_over_the_segment_cap_is_refused_not_truncated(client, config):
    seed_many(config, MAX_SEGMENTS + 1)
    for r in segment_calls(client):
        assert r.status_code == 400
        assert "50000" in r.json()["error"]


def test_transcript_at_the_segment_cap_returns_all(client, config):
    seed_many(config, MAX_SEGMENTS)
    got = list(segment_calls(client))
    assert [r.status_code for r in got] == [200] * 4
    assert len(got[0].json()["segments"]) == MAX_SEGMENTS
    assert len(got[1].json()["segments"]) == MAX_SEGMENTS
    assert got[2].text.count("\n") == MAX_SEGMENTS
    assert got[3].text.count("\n") == MAX_SEGMENTS


def test_segment_queries_stop_reading_at_the_limit(config):
    seed_many(config, MAX_SEGMENTS + 100)
    limit = MAX_SEGMENTS + 1
    assert (
        len(queries.segments(config.db_path, T0 - 1, T0 + 10_000_000, limit)) == limit
    )
    day = queries.local_date(T0, config.zone)
    assert len(queries.day_segments(config.db_path, day, config.zone, limit)) == limit


def test_days_scan_holds_memory_per_day_not_per_segment(config):
    n = 200_000
    seed_many(config, n)
    tracemalloc.start()
    try:
        got = queries.days(config.db_path, None, None, config.zone)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert got == [
        {
            "date": DAY,
            "segments": n,
            "first_ms": T0,
            "last_ms": T0 + (n - 1) * 100 + 50,
            "sources": ["limitless", "pendant"],
        }
    ]
    assert peak < 8 << 20


@pytest.mark.parametrize(
    ("zone", "day", "other", "clock"),
    [
        ("Asia/Tokyo", "2026-10-02", "2026-10-01", "08:08:02"),
        ("America/Los_Angeles", "2026-10-01", "2026-10-02", "16:08:02"),
    ],
)
def test_c5_every_reader_of_a_day_cuts_it_in_the_configured_zone(
    tmp_path, zone, day, other, clock
):
    config = Config(
        tokens={UP: "upload", RD: "read"}, data_dir=tmp_path, zone=ZoneInfo(zone)
    )
    store.init(config.db_path)
    conn = store.connect(config.db_path)
    conn.execute(
        "INSERT INTO segments (start_ms, end_ms, text, source, speaker, name)"
        " VALUES (?, ?, 'words', 'pendant', 'S1', NULL)",
        (T0, T0 + 4000),
    )
    conn.execute(
        "INSERT INTO limitless_lifelogs VALUES ('L1','t',?,?,0,'md','{}')",
        (T0, T0 + 4000),
    )
    conn.close()
    client = TestClient(create_app(config))
    md = {**rd(), "Accept": "text/markdown"}

    assert [d["date"] for d in client.get("/v1/days", headers=rd()).json()] == [day]
    assert client.get(f"/v1/days/{day}/transcript", headers=md).text.splitlines() == [
        f"- {clock} **S1** words"
    ]
    got = client.get(f"/v1/days/{day}/limitless", headers=rd()).json()
    assert [x["lifelog_id"] for x in got] == ["L1"]
    assert (
        client.get(f"/v1/days/{other}/transcript", headers=rd()).json()["segments"]
        == []
    )
    assert client.get(f"/v1/days/{other}/limitless", headers=rd()).json() == []
