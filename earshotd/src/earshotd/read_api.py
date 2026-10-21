"""Read API routes under /v1: days, transcripts, Limitless originals, pendant records, health, status."""

from __future__ import annotations

import datetime as dt
from datetime import tzinfo
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from . import queries
from .auth import require

MAX_SEGMENTS = 50_000

router = APIRouter()
_read = [Depends(require("read"))]


class Segment(BaseModel):
    start_ms: int
    end_ms: int
    text: str
    source: Literal["pendant", "limitless"]
    speaker: str | None
    name: str | None


class Day(BaseModel):
    date: str
    segments: int
    first_ms: int
    last_ms: int
    sources: list[str]


class Transcript(BaseModel):
    date: str | None = None
    segments: list[Segment]


class Lifelog(BaseModel):
    lifelog_id: str
    title: str | None
    start_ms: int
    end_ms: int
    starred: bool
    markdown: str | None


class PendantRecord(BaseModel):
    kind: str
    ts_ms: int | None  # None for a CRC-failed page, which carries no timestamp
    session: int
    page_index: int
    data: dict


def _date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, f"not a YYYY-MM-DD date: {value!r}") from None


def _within_cap(segs: list[dict]) -> None:
    """`segs` was asked for with limit MAX_SEGMENTS + 1: one extra row means the range is too big."""
    if len(segs) > MAX_SEGMENTS:
        raise HTTPException(
            400, f"more than {MAX_SEGMENTS} segments; ask for a shorter range"
        )


def _markdown(segments: list[dict], zone: tzinfo) -> str:
    return "".join(
        f"- {queries.local_clock(s['start_ms'], zone)}"
        f" **{s['name'] or s['speaker'] or 'unknown'}** {s['text']}\n"
        for s in segments
    )


@router.get("/v1/days", dependencies=_read, response_model=list[Day])
async def list_days(
    request: Request,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: str | None = None,
) -> list[dict]:
    first = _date(from_) if from_ else None
    last = _date(to) if to else None
    if first and last and first > last:
        raise HTTPException(400, "from is after to")
    return await run_in_threadpool(
        queries.days,
        request.app.state.config.db_path,
        first,
        last,
        request.app.state.config.zone,
    )


@router.get("/v1/days/{date}/transcript", dependencies=_read, response_model=Transcript)
async def day_transcript(request: Request, date: str):
    d = _date(date)
    segs = await run_in_threadpool(
        queries.day_segments,
        request.app.state.config.db_path,
        d,
        request.app.state.config.zone,
        MAX_SEGMENTS + 1,
    )
    _within_cap(segs)
    if "text/markdown" in request.headers.get("accept", ""):
        return PlainTextResponse(
            _markdown(segs, request.app.state.config.zone), media_type="text/markdown"
        )
    return {"date": d.isoformat(), "segments": segs}


@router.get("/v1/transcript", dependencies=_read, response_model=Transcript)
async def range_transcript(
    request: Request, from_: Annotated[int, Query(alias="from")], to: int
):
    if from_ > to:
        raise HTTPException(400, "from is after to")
    segs = await run_in_threadpool(
        queries.segments,
        request.app.state.config.db_path,
        from_,
        to,
        MAX_SEGMENTS + 1,
    )
    _within_cap(segs)
    if "text/markdown" in request.headers.get("accept", ""):
        return PlainTextResponse(
            _markdown(segs, request.app.state.config.zone), media_type="text/markdown"
        )
    return {"segments": segs}


@router.get(
    "/v1/days/{date}/limitless", dependencies=_read, response_model=list[Lifelog]
)
async def day_limitless(request: Request, date: str) -> list[dict]:
    return await run_in_threadpool(
        queries.limitless,
        request.app.state.config.db_path,
        _date(date),
        request.app.state.config.zone,
    )


@router.get(
    "/v1/pendant/records", dependencies=_read, response_model=list[PendantRecord]
)
async def pendant_records(
    request: Request,
    kind: str | None = None,
    from_: Annotated[int | None, Query(alias="from")] = None,
    to: int | None = None,
    limit: Annotated[int, Query(ge=1, le=10000)] = 1000,
) -> list[dict]:
    if from_ is not None and to is not None and from_ > to:
        raise HTTPException(400, "from is after to")
    return await run_in_threadpool(
        queries.pendant_records,
        request.app.state.config.db_path,
        kind,
        from_,
        to,
        limit,
    )


@router.get("/v1/health")
async def health(request: Request) -> dict:
    """Liveness for the uptime monitor: no token, no data. A store that will not open is a 500."""
    await run_in_threadpool(queries.alive, request.app.state.config.db_path)
    return {"ok": True}


@router.get("/v1/status", dependencies=_read)
async def status(request: Request) -> dict:
    worker_status = getattr(request.app.state, "worker_status", None)
    return await run_in_threadpool(
        queries.health,
        request.app.state.config.db_path,
        worker_status.as_dict() if worker_status else None,
    )
