"""`earshot import-limitless`: pull the Limitless archive into earshotd's store.

DESIGN.md, 'Limitless import'. Steps 1-3: every lifelog is upserted, then each lifelog's audio is
downloaded in chunks of at most 2 hours and written through `store.store_audio`. A chunk that is
already committed is skipped, so an interrupted import resumes. Nothing is skipped silently: a
lifelog that still fails after the client's retries is named in the report and the run is not ok.
"""

from __future__ import annotations

import io
import json
import struct
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import NamedTuple

import av

from . import store
from .config import Config
from .limitless import MAX_RANGE_MS, Client, LimitlessError, NoAudio

EXPECTED_RATE = 16000
_EPOCH = datetime.fromisoformat("1970-01-01T00:00:00+00:00")


@dataclass
class Report:
    listed: int = 0  # lifelogs the API listed
    stored: int = 0  # lifelogs in limitless_lifelogs afterwards
    chunks_downloaded: int = 0
    chunks_skipped: int = 0  # already stored by an earlier run
    failures: dict[str, str] = field(default_factory=dict)  # lifelog_id -> why
    no_audio_at_limitless: list[str] = field(
        default_factory=list
    )  # lifelog_ids Limitless has no audio for
    no_audio: list[str] = field(default_factory=list)  # lifelog_ids with no time range

    @property
    def ok(self) -> bool:
        return not self.failures and self.listed > 0 and self.listed == self.stored


class Span(NamedTuple):
    """What the audio pass needs of a lifelog: its id and time range."""

    lifelog_id: str
    start_ms: int
    end_ms: int


def to_ms(stamp: str) -> int:
    """Epoch ms of an ISO 8601 timestamp that carries its UTC offset."""
    when = datetime.fromisoformat(stamp)
    if when.tzinfo is None:
        raise ValueError(f"timestamp without a UTC offset: {stamp!r}")
    return (when - _EPOCH) // timedelta(milliseconds=1)


def parse_lifelog(raw: dict) -> store.Lifelog:
    return store.Lifelog(
        lifelog_id=str(raw["id"]),
        title=raw.get("title"),
        start_ms=to_ms(raw["startTime"]),
        end_ms=to_ms(raw["endTime"]),
        starred=bool(raw.get("isStarred")),
        markdown=raw.get("markdown") or "",
        contents_json=json.dumps(raw.get("contents") or []),
    )


def chunks(start_ms: int, end_ms: int) -> list[tuple[int, int]]:
    """[start_ms, end_ms] cut into consecutive ranges of at most 2 hours."""
    out = []
    at = start_ms
    while at < end_ms:
        out.append((at, min(at + MAX_RANGE_MS, end_ms)))
        at = out[-1][1]
    return out


def check_audio(data: bytes) -> str | None:
    """Why `data` is not Ogg Opus, 16 kHz, mono that PyAV decodes; None when it is.

    PyAV reports 48 kHz for every Opus stream, so the original rate is read from OpusHead.
    """
    try:
        with av.open(io.BytesIO(data)) as c:
            assert isinstance(c, av.container.InputContainer)
            stream = c.streams.audio[0]
            ctx = stream.codec_context
            head = bytes(ctx.extradata or b"")
            if ctx.name != "opus" or head[:8] != b"OpusHead" or len(head) < 16:
                return f"not Opus audio (codec {ctx.name})"
            channels = head[9]
            (rate,) = struct.unpack_from("<I", head, 12)
            if channels != 1:
                return f"{channels} channels, expected mono"
            if rate != EXPECTED_RATE:
                return f"{rate} Hz, expected {EXPECTED_RATE}"
            samples = sum(int(frame.samples) for frame in c.decode(stream))
            if samples == 0:
                return "no audio decoded"
    except (av.error.FFmpegError, IndexError, ValueError, OSError) as e:
        return f"does not decode ({type(e).__name__})"
    return None


def run(
    config: Config,
    client: Client,
    out: Callable[[str], None] = lambda m: print(m, file=sys.stderr),
) -> Report:
    """Import everything the API lists; `out` receives progress lines. A listing that fails raises `LimitlessError`: without
    the list there is nothing to resume from, and the run exits non-zero."""
    report = Report()
    listed: list[Span] = []  # only ids and ranges outlive a page: markdown is not held
    for page in client.lifelog_pages():
        batch = [parse_lifelog(raw) for raw in page]
        store.upsert_lifelogs(config.db_path, batch)
        listed.extend(Span(x.lifelog_id, x.start_ms, x.end_ms) for x in batch)
        report.listed += len(batch)
        out(f"listed {report.listed} lifelogs")
    for n, lifelog in enumerate(listed, start=1):
        _import_audio(config, client, lifelog, report)
        if n % 25 == 0:
            out(f"audio: {n} of {len(listed)} lifelogs done")
    report.stored = store.count_lifelogs(config.db_path)
    return report


def _import_audio(
    config: Config, client: Client, lifelog: Span, report: Report
) -> None:
    ranges = chunks(lifelog.start_ms, lifelog.end_ms)
    if not ranges:
        report.no_audio.append(lifelog.lifelog_id)
        return
    for start, end in ranges:
        if store.audio_stored(
            config.db_path, lifelog.lifelog_id, start, end
        ) or store.audio_absent(config.db_path, lifelog.lifelog_id, start, end):
            report.chunks_skipped += 1
            continue
        try:
            data = client.download_audio(start, end)
            problem = check_audio(data)
            if problem:
                raise LimitlessError(f"audio {start}-{end}: {problem}")
        except NoAudio as e:
            store.store_audio_absent(
                config.db_path,
                lifelog.lifelog_id,
                start,
                end,
                e.text,
                int(time.time() * 1000),
            )
            if lifelog.lifelog_id not in report.no_audio_at_limitless:
                report.no_audio_at_limitless.append(lifelog.lifelog_id)
            continue
        except LimitlessError as e:
            report.failures[lifelog.lifelog_id] = str(e)
            return
        store.store_audio(
            config.db_path, config.data_dir, lifelog.lifelog_id, start, end, data
        )
        report.chunks_downloaded += 1


def summary(report: Report) -> str:
    lines = [
        f"limitless: API listed {report.listed} lifelogs, stored {report.stored}",
        f"audio chunks: {report.chunks_downloaded} downloaded, {report.chunks_skipped} already stored",
    ]
    if report.no_audio:
        lines.append(
            f"no audio (empty time range), {len(report.no_audio)}: "
            + ", ".join(report.no_audio)
        )
    if report.no_audio_at_limitless:
        lines.append(
            f"no audio at Limitless, {len(report.no_audio_at_limitless)}: "
            + ", ".join(report.no_audio_at_limitless)
        )
    if report.listed == 0:
        lines.append("FAILED: the API listed no lifelogs")
    elif report.listed != report.stored:
        lines.append("FAILED: the stored count does not match the API's")
    if report.failures:
        lines.append(f"FAILED: {len(report.failures)} lifelogs incomplete:")
        lines += [f"  {lid}: {why}" for lid, why in sorted(report.failures.items())]
    return "\n".join(lines)
