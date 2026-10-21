"""A small client for the two Limitless endpoints the import uses: /v1/lifelogs and /v1/download-audio.

Cloudflare in front of api.limitless.ai answers Python's default urllib User-Agent with 403, so
every request carries an explicit one. 429 and 5xx (and a dropped connection) are retried with
backoff; every other failure raises `LimitlessError` at once (a 404 whose body is exactly the
no-audio error raises its subclass `NoAudio`), so a 403 can never read as an empty
archive.

The key goes only to the base the client was given: the base must be https (plain http only to
127.0.0.1, localhost or ::1) and carry no userinfo, query or fragment, and no redirect is
followed, so a 3xx fails the call instead of sending the key to the host a Location names.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator

BASE_URL = "https://api.limitless.ai"
USER_AGENT = "earshot-import/1.0"
PAGE_SIZE = 10  # the most /v1/lifelogs returns per request
MAX_RANGE_MS = 2 * 3_600_000  # /v1/download-audio: "Time range cannot exceed 2 hours"
MAX_BACKOFF_S = 120.0
# The hosts plain http may reach; app/Sources/EarshotCore/Config.swift states the same set.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class LimitlessError(Exception):
    """A request that failed for good. The message never carries the API key."""


NO_AUDIO_TEXT = "No audio available for the requested time range"


class NoAudio(LimitlessError):
    """Limitless answered 404 with exactly {"error": NO_AUDIO_TEXT}: it holds no audio for the range."""

    def __init__(self, message: str, text: str) -> None:
        super().__init__(message)
        self.text = text


def _is_no_audio(body: bytes) -> bool:
    try:
        doc = json.loads(body)
    except ValueError:
        return False
    return isinstance(doc, dict) and doc == {"error": NO_AUDIO_TEXT}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Follow no 3xx: urllib then raises the HTTPError itself."""

    def redirect_request(self, *_a, **_k):  # type: ignore[no-untyped-def]
        return None


def _check_base(base_url: str) -> None:
    """Refuse a base the key must not be sent to; the message shows only scheme and host."""
    try:
        parts = urllib.parse.urlsplit(base_url)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        raise LimitlessError("Limitless base is not a valid URL") from None
    shown = f"{parts.scheme}://{host or ''}"
    if "@" in parts.netloc or parts.query or parts.fragment:
        raise LimitlessError(
            f"Limitless base {shown} refused: it must carry no userinfo, query or fragment"
        )
    if parts.scheme == "https":
        ok = bool(host)
    elif parts.scheme == "http":
        ok = host in LOOPBACK_HOSTS
    else:
        ok = False
    if not ok:
        raise LimitlessError(
            f"Limitless base {shown} refused: it must be https, or http to"
            " 127.0.0.1, localhost or ::1"
        )


class Client:
    def __init__(
        self,
        api_key: str,
        base_url: str = BASE_URL,
        sleep: Callable[[float], None] = time.sleep,
        attempts: int = 6,
        base_delay_s: float = 1.0,
        timeout_s: float = 120.0,
    ) -> None:
        _check_base(base_url)
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._sleep = sleep
        self._attempts = attempts
        self._base_delay = base_delay_s
        self._timeout = timeout_s
        self.user_agent = USER_AGENT
        self._opener = urllib.request.build_opener(_NoRedirect())

    def lifelog_pages(self) -> Iterator[list[dict]]:
        """Every page of lifelogs, oldest first, with markdown, headings and contents."""
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            params = {
                "limit": str(PAGE_SIZE),
                "direction": "asc",
                "includeMarkdown": "true",
                "includeHeadings": "true",
                "includeContents": "true",
            }
            if cursor is not None:
                params["cursor"] = cursor
            body = self._get("/v1/lifelogs", params)
            try:
                doc = json.loads(body)
                page = doc["data"]["lifelogs"]
                meta = doc.get("meta", {}).get("lifelogs", {})
                cursor = meta.get("nextCursor")
            except (ValueError, KeyError, TypeError, AttributeError):
                raise LimitlessError(
                    "/v1/lifelogs answered 200 without data.lifelogs"
                ) from None
            if not isinstance(page, list):
                raise LimitlessError("/v1/lifelogs: data.lifelogs is not a list")
            yield page
            if not cursor:
                return
            if cursor in seen:
                raise LimitlessError(f"/v1/lifelogs repeated its cursor {cursor!r}")
            seen.add(cursor)

    def download_audio(self, start_ms: int, end_ms: int) -> bytes:
        """The pendant's audio for [start_ms, end_ms], at most 2 hours, as Ogg Opus bytes."""
        return self._get(
            "/v1/download-audio",
            {"startMs": str(start_ms), "endMs": str(end_ms), "audioSource": "pendant"},
        )

    def _get(self, path: str, params: dict[str, str]) -> bytes:
        url = f"{self._base}{path}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url, headers={"X-API-Key": self._key, "User-Agent": self.user_agent}
        )
        for attempt in range(1, self._attempts + 1):
            wait = self._base_delay * 2 ** (attempt - 1)
            try:
                with self._opener.open(request, timeout=self._timeout) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                if e.code == 404 and _is_no_audio(e.read()):
                    raise NoAudio(
                        f"{path}: HTTP 404, {NO_AUDIO_TEXT}", NO_AUDIO_TEXT
                    ) from None
                if e.code != 429 and e.code < 500:
                    raise LimitlessError(f"{path}: HTTP {e.code}") from None
                problem = f"HTTP {e.code}"
                retry_after = e.headers.get("Retry-After") if e.headers else None
                if retry_after and retry_after.isdigit():
                    wait = max(wait, float(retry_after))
            except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
                problem = type(e).__name__
            if attempt == self._attempts:
                raise LimitlessError(f"{path}: {problem} after {attempt} attempts")
            self._sleep(min(wait, MAX_BACKOFF_S))
        raise AssertionError("unreachable")
