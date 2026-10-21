"""A local stand-in for api.limitless.ai: /v1/lifelogs and /v1/download-audio, shaped like the real ones.

It behaves like the real service where the importer depends on it: `X-API-Key` is checked, a
request without an explicit User-Agent gets Cloudflare's 403, lifelogs page by cursor at most 10
at a time, and audio ranges over 2 hours are refused. Faults (429, 5xx) are scripted per test.
"""

from __future__ import annotations

import io
import json
import threading
from datetime import UTC, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import av
import numpy as np

KEY = "test-key-not-a-real-limitless-key"
HOUR_MS = 3_600_000
NO_AUDIO_BODY = {"error": "No audio available for the requested time range"}


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone(timedelta(hours=-7))).isoformat(
        timespec="milliseconds"
    )


def ogg_opus(seconds: float, rate: int = 16000, channels: int = 1) -> bytes:
    """A real Ogg Opus file of a quiet tone: what download-audio returns."""
    buf = io.BytesIO()
    out = av.open(buf, "w", format="ogg")
    layout = "mono" if channels == 1 else "stereo"
    stream = out.add_stream("libopus", rate=rate, layout=layout)
    n = int(rate * seconds)
    tone = (0.2 * np.sin(2 * np.pi * 220 * np.arange(n) / rate)).astype(np.float32)
    data = np.tile(tone, (channels, 1))
    for at in range(0, n, rate):  # one second per frame handed to the encoder
        frame = av.AudioFrame.from_ndarray(
            np.ascontiguousarray(data[:, at : at + rate]), format="fltp", layout=layout
        )
        frame.sample_rate = rate
        for packet in stream.encode(frame):
            out.mux(packet)
    for packet in stream.encode(None):
        out.mux(packet)
    out.close()
    return buf.getvalue()


def lifelog(n: int, start_ms: int, end_ms: int, **extra) -> dict:
    """A lifelog as /v1/lifelogs returns it with includeMarkdown/includeHeadings/includeContents."""
    title = extra.pop("title", f"Conversation {n}")
    starred = bool(extra.pop("starred", False))
    return {
        "id": f"ll{n:05d}",
        "title": title,
        "markdown": f"# {title}\n\n- Unknown: line one of {n}\n- Unknown: line two of {n}\n",
        "startTime": iso(start_ms),
        "endTime": iso(end_ms),
        "isStarred": starred,
        "updatedAt": iso(end_ms),
        "contents": [
            {
                "type": "heading1",
                "content": title,
                "startTime": iso(start_ms),
                "endTime": iso(end_ms),
                "children": [],
            },
            {
                "type": "blockquote",
                "content": f"line one of {n}",
                "startTime": iso(start_ms),
                "endTime": iso(end_ms),
                "speakerName": "Unknown",
                "speakerIdentifier": None,
            },
        ],
        **extra,
    }


MEETING_MS = int(datetime(2025, 11, 4, 19, 0, 23, tzinfo=UTC).timestamp() * 1000)
MEETING_TITLE = "Team meeting"


def standard_archive(extra: int = 22) -> list[dict]:
    """The team meeting of 2025-11-04 19:00:23Z, a 5 hour lifelog (three chunks), and `extra`
    ordinary conversations an hour apart on the following days."""
    out = [
        lifelog(
            0, MEETING_MS, MEETING_MS + 45 * 60_000, title=MEETING_TITLE, starred=True
        ),
        lifelog(
            1, MEETING_MS + 2 * HOUR_MS, MEETING_MS + 7 * HOUR_MS, title="Long drive"
        ),
    ]
    day2 = MEETING_MS + 24 * HOUR_MS
    for i in range(extra):
        s = day2 + i * HOUR_MS
        out.append(lifelog(2 + i, s, s + 20 * 60_000))
    return out


class FakeLimitless:
    def __init__(
        self,
        lifelogs: list[dict],
        audio_seconds: float = 2.0,
        audio_delay_s: float = 0.0,
        audio_rate: int = 16000,
    ) -> None:
        self.lifelogs = lifelogs
        self.base_url = ""
        self.audio_seconds = audio_seconds
        self.audio_delay_s = audio_delay_s
        self.audio_rate = audio_rate
        self.requests: list[tuple[str, dict[str, str]]] = []  # (path, query)
        self.user_agents: list[str | None] = []
        self.keys: list[str | None] = []
        # Scripted faults: each list is consumed one status per matching request.
        self.list_faults: list[int] = []
        self.audio_faults: dict[
            int, list[int]
        ] = {}  # startMs -> statuses, one per request
        self.always_fail_audio: dict[
            int, int
        ] = {}  # startMs -> status, on every request
        self.no_audio_at: set[int] = (
            set()
        )  # startMs -> the real 404 "No audio available"
        self.audio_errors: dict[
            int, tuple[int, object]
        ] = {}  # startMs -> (status, JSON body), on every request
        # (status, Location): when set, every request answers with this redirect.
        self.redirect: tuple[int, str] | None = None
        self.lock = threading.Lock()
        self._clips: dict[tuple[int, int], bytes] = {}
        self._server: ThreadingHTTPServer | None = None

    # -- the HTTP face -------------------------------------------------------------------

    def start(self) -> str:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a) -> None:
                pass

            def do_GET(self) -> None:
                fake._handle(self)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"
        return self.base_url

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    def paths(self) -> set[str]:
        return {p for p, _ in self.requests}

    def audio_requests(self) -> list[tuple[int, int]]:
        return [
            (int(q["startMs"]), int(q["endMs"]))
            for p, q in list(self.requests)
            if p == "/v1/download-audio"
        ]

    def _reply(
        self, h: BaseHTTPRequestHandler, status: int, body: bytes, ctype: str
    ) -> None:
        h.send_response(status)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    def _json(self, h: BaseHTTPRequestHandler, status: int, obj) -> None:
        self._reply(h, status, json.dumps(obj).encode(), "application/json")

    def _handle(self, h: BaseHTTPRequestHandler) -> None:
        url = urlparse(h.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        with self.lock:
            self.requests.append((url.path, query))
            self.user_agents.append(h.headers.get("User-Agent"))
            self.keys.append(h.headers.get("X-API-Key"))
        if self.redirect:
            status, location = self.redirect
            h.send_response(status)
            h.send_header("Location", location)
            h.send_header("Content-Length", "0")
            h.end_headers()
            return
        ua = h.headers.get("User-Agent") or ""
        if not ua or ua.startswith("Python-urllib"):
            return self._reply(h, 403, b"<html>error code: 1010</html>", "text/html")
        if h.headers.get("X-API-Key") != KEY:
            return self._json(h, 401, {"error": "invalid api key"})
        if url.path == "/v1/lifelogs":
            return self._lifelogs(h, query)
        if url.path == "/v1/download-audio":
            return self._audio(h, query)
        self._json(h, 404, {"error": "not found"})

    def _fault(self, queue: list[int]) -> int | None:
        with self.lock:
            return queue.pop(0) if queue else None

    def _lifelogs(self, h: BaseHTTPRequestHandler, q: dict[str, str]) -> None:
        status = self._fault(self.list_faults)
        if status:
            return self._json(h, status, {"error": "scripted"})
        limit = min(int(q.get("limit", "3")), 10)
        at = int(q["cursor"]) if "cursor" in q else 0
        ordered = sorted(self.lifelogs, key=lambda x: x["startTime"])
        page = ordered[at : at + limit]
        nxt = at + limit
        body = {
            "data": {"lifelogs": page},
            "meta": {
                "lifelogs": {
                    "nextCursor": str(nxt) if nxt < len(ordered) else None,
                    "count": len(page),
                }
            },
        }
        self._json(h, 200, body)

    def _audio(self, h: BaseHTTPRequestHandler, q: dict[str, str]) -> None:
        start, end = int(q["startMs"]), int(q["endMs"])
        if q.get("audioSource") != "pendant":
            return self._json(h, 400, {"error": "audioSource must be pendant"})
        if end - start > 2 * HOUR_MS:
            return self._json(h, 400, {"error": "Time range cannot exceed 2 hours"})
        if self.audio_delay_s:
            threading.Event().wait(self.audio_delay_s)
        if start in self.no_audio_at:
            return self._json(h, 404, NO_AUDIO_BODY)
        if start in self.audio_errors:
            err_status, err_body = self.audio_errors[start]
            return self._json(h, err_status, err_body)
        status = self.always_fail_audio.get(start)
        if status is None:
            with self.lock:
                queue = self.audio_faults.setdefault(start, [])
            status = self._fault(queue)
        if status:
            return self._json(h, status, {"error": "scripted"})
        seconds = min((end - start) / 1000, self.audio_seconds)
        key = (int(seconds * 1000), self.audio_rate)
        with self.lock:
            clip = self._clips.get(key)
            if clip is None:
                clip = self._clips[key] = ogg_opus(seconds, self.audio_rate)
        self._reply(h, 200, clip, "audio/ogg")
