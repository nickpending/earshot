import asyncio
import json

from conftest import page_count, up

from earshotd import app as app_module
from earshotd.app import create_app


def test_fixture_stored(client, config, body):
    r = client.post("/v1/pages", content=body, headers=up())
    assert r.status_code == 200
    assert r.json() == {"stored": 36, "duplicates": 0}
    assert page_count(config) == 36


def test_resend_is_duplicate(client, config, body):
    client.post("/v1/pages", content=body, headers=up())
    r = client.post("/v1/pages", content=body, headers=up())
    assert r.status_code == 200
    assert r.json() == {"stored": 0, "duplicates": 36}
    assert page_count(config) == 36


def test_same_index_new_timestamp_is_a_new_page(client, config, body):
    # Page indices restart after a factory reset: the timestamp is part of the key.
    client.post("/v1/pages", content=body, headers=up())
    changed = bytearray(body)
    ts = int.from_bytes(changed[8:16], "little") + 1
    changed[8:16] = ts.to_bytes(8, "little")
    r = client.post("/v1/pages", content=bytes(changed), headers=up())
    assert r.json() == {"stored": 1, "duplicates": 35}
    assert page_count(config) == 37


def test_empty_body_is_ok(client):
    r = client.post("/v1/pages", content=b"", headers=up())
    assert r.status_code == 200
    assert r.json() == {"stored": 0, "duplicates": 0}


def test_wrong_content_type_rejected(client, config, body):
    r = client.post(
        "/v1/pages", content=body, headers=up(ctype="application/octet-stream")
    )
    assert r.status_code == 400
    assert "error" in r.json()
    assert page_count(config) == 0


def test_timestamp_beyond_signed_64_bit_is_400(client, config, body):
    bad = bytearray(body)
    bad[8:16] = (2**63).to_bytes(8, "little")
    r = client.post("/v1/pages", content=bytes(bad), headers=up())
    assert r.status_code == 400
    assert "error" in r.json()
    assert page_count(config) == 0


def test_unknown_route_and_wrong_method_use_error_shape(client):
    r = client.get("/v1/nope")
    assert r.status_code == 404
    assert "error" in r.json() and "detail" not in r.json()
    r = client.get("/v1/pages")
    assert r.status_code == 405
    assert "error" in r.json() and "detail" not in r.json()


# Memory bounds: size cap, upload slots, read timeout, driven through the real ASGI app.

MAX_BODY = 4 << 20
CHUNK = 64 << 10
V2 = "application/vnd.earshot.pages.v2"


class Wire:
    """A receive callable that hands out chunks and counts what it handed out."""

    def __init__(self, chunks, hold=None, stall=False):
        self.chunks = list(chunks)
        self.consumed = 0
        self.messages = 0
        self.entered = asyncio.Event()
        self.hold = hold  # an Event every chunk after the first waits for
        self.stall = stall  # never send anything after the first chunk

    async def __call__(self):
        self.messages += 1
        self.entered.set()
        if self.messages > 1 and self.stall:
            await asyncio.Event().wait()
        if self.messages > 1 and self.hold is not None:
            await self.hold.wait()
        if not self.chunks:
            return {"type": "http.disconnect"}
        chunk = self.chunks.pop(0)
        self.consumed += len(chunk)
        return {"type": "http.request", "body": chunk, "more_body": bool(self.chunks)}


def chunked(body: bytes, size: int = CHUNK) -> list[bytes]:
    return [body[i : i + size] for i in range(0, len(body), size)] or [b""]


async def asgi_post(app, wire, headers, path="/v1/pages"):
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 80),
    }
    start: dict = {}
    parts: list[bytes] = []

    async def send(message):
        if message["type"] == "http.response.start":
            start.update(message)
        elif message["type"] == "http.response.body":
            parts.append(message.get("body", b""))

    await app(scope, wire, send)
    got = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    return start["status"], got, b"".join(parts)


def test_declared_length_over_cap_is_413_with_no_body_read(config):
    app = create_app(config)
    wire = Wire(chunked(b"\xff" * 1000))
    headers = {**up(ctype=V2), "Content-Length": str(MAX_BODY + 1)}
    status, _, out = asyncio.run(asgi_post(app, wire, headers))
    assert status == 413
    assert "error" in json.loads(out)
    assert wire.messages == 0
    assert page_count(config) == 0


def test_streamed_body_over_cap_is_413_after_at_most_cap_plus_a_chunk(config):
    app = create_app(config)
    wire = Wire(chunked(b"\xff" * (8 << 20)))
    status, _, out = asyncio.run(asgi_post(app, wire, up(ctype=V2)))
    assert status == 413
    assert "error" in json.loads(out)
    assert wire.consumed <= MAX_BODY + CHUNK
    assert page_count(config) == 0


def test_size_boundary_is_exactly_the_cap(config):
    # 0xff bytes parse as a v2 length that overruns the body: a fast 400 once read in full.
    app = create_app(config)
    at = b"\xff" * MAX_BODY
    headers = {**up(ctype=V2), "Content-Length": str(len(at))}
    status, _, _ = asyncio.run(asgi_post(app, Wire(chunked(at)), headers))
    assert status != 413
    over = at + b"\xff"
    headers = {**up(ctype=V2), "Content-Length": str(len(over))}
    status, _, _ = asyncio.run(asgi_post(app, Wire(chunked(over)), headers))
    assert status == 413


def test_third_upload_is_503_and_slots_free_on_every_outcome(config, body):
    app = create_app(config)

    async def scenario():
        release_ok, release_bad = asyncio.Event(), asyncio.Event()
        ok = Wire([body[:10], body[10:]], hold=release_ok)
        bad = Wire([b"\x01", b"\x02"], hold=release_bad)  # truncated header: a 400
        t_ok = asyncio.create_task(asgi_post(app, ok, up()))
        t_bad = asyncio.create_task(asgi_post(app, bad, up()))
        await ok.entered.wait()
        await bad.entered.wait()
        third = Wire(chunked(body))
        refused = await asgi_post(app, third, up())
        release_ok.set()
        release_bad.set()
        first, second = await t_ok, await t_bad
        # both slots must be free again: two more uploads hold them at once
        release = asyncio.Event()
        held = [Wire([body[:10], body[10:]], hold=release) for _ in range(2)]
        tasks = [asyncio.create_task(asgi_post(app, w, up())) for w in held]
        for w, t in zip(held, tasks):  # a refused upload ends without entering
            entered = asyncio.ensure_future(w.entered.wait())
            await asyncio.wait({entered, t}, return_when=asyncio.FIRST_COMPLETED)
        release.set()
        fourth = [await t for t in tasks]
        return third, refused, first, second, fourth

    third, refused, first, second, fourth = asyncio.run(scenario())
    assert refused[0] == 503
    assert "retry-after" in refused[1]
    assert "error" in json.loads(refused[2])
    assert third.messages == 0
    assert (first[0], second[0]) == (200, 400)
    assert [f[0] for f in fourth] == [200, 200]


def test_stalled_upload_is_408_and_frees_its_slot(config, body, monkeypatch):
    assert app_module.UPLOAD_READ_SECONDS == 120
    monkeypatch.setattr(app_module, "UPLOAD_READ_SECONDS", 0.2)
    app = create_app(config)

    async def scenario():
        stalled = await asgi_post(app, Wire([body[:10], body[10:]], stall=True), up())
        follow = await asgi_post(app, Wire(chunked(body)), up())
        return stalled, follow

    stalled, follow = asyncio.run(asyncio.wait_for(scenario(), 10))
    assert stalled[0] == 408
    assert "error" in json.loads(stalled[2])
    assert follow[0] == 200
    assert page_count(config) == 36


def test_v1_body_over_1_mib_is_accepted_with_every_page_counted(client, body):
    copies = (1 << 20) // len(body) + 2
    r = client.post("/v1/pages", content=body * copies, headers=up())
    assert r.status_code == 200
    assert r.json()["stored"] + r.json()["duplicates"] == 36 * copies
