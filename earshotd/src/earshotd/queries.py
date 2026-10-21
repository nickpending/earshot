"""Read-side SQL: days, segments, Limitless originals, liveness, health. A day is a date in the zone given."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, tzinfo
from pathlib import Path

from . import store

_SEGMENT_COLS = "start_ms, end_ms, text, source, speaker, name"


def day_bounds(day: date, zone: tzinfo) -> tuple[int, int]:
    """[start, end) epoch ms of a calendar day in `zone`."""
    start = datetime.combine(day, time.min, zone)
    end = datetime.combine(day + timedelta(days=1), time.min, zone)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def local_date(ms: int, zone: tzinfo) -> date:
    return datetime.fromtimestamp(ms / 1000, zone).date()


def local_clock(ms: int, zone: tzinfo) -> str:
    return datetime.fromtimestamp(ms / 1000, zone).strftime("%H:%M:%S")


def days(
    db_path: str | Path, first: date | None, last: date | None, zone: tzinfo
) -> list[dict]:
    lo = day_bounds(first, zone)[0] if first else None
    hi = day_bounds(last, zone)[1] if last else None
    sql, args = "SELECT start_ms, end_ms, source FROM segments", []
    clauses = []
    if lo is not None:
        clauses.append("start_ms >= ?")
        args.append(lo)
    if hi is not None:
        clauses.append("start_ms < ?")
        args.append(hi)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    conn = store.connect(db_path)
    try:
        out: dict[date, dict] = {}
        for start, end, source in conn.execute(sql, args):
            d = out.setdefault(
                local_date(start, zone),
                {"segments": 0, "first_ms": start, "last_ms": end, "sources": set()},
            )
            d["segments"] += 1
            d["first_ms"] = min(d["first_ms"], start)
            d["last_ms"] = max(d["last_ms"], end)
            d["sources"].add(source)
    finally:
        conn.close()
    return [
        {
            "date": day.isoformat(),
            "segments": v["segments"],
            "first_ms": v["first_ms"],
            "last_ms": v["last_ms"],
            "sources": sorted(v["sources"]),
        }
        for day, v in sorted(out.items(), reverse=True)
    ]


def _segments(db_path: str | Path, where: str, args: tuple, limit: int) -> list[dict]:
    conn = store.connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT {_SEGMENT_COLS} FROM segments WHERE {where}"
            " ORDER BY start_ms, id LIMIT ?",
            (*args, limit),
        ).fetchall()
    finally:
        conn.close()
    keys = [c.strip() for c in _SEGMENT_COLS.split(",")]
    return [dict(zip(keys, r)) for r in rows]


def segments(db_path: str | Path, lo_ms: int, hi_ms: int, limit: int) -> list[dict]:
    """At most `limit` segments overlapping [lo_ms, hi_ms), in time order."""
    return _segments(db_path, "end_ms > ? AND start_ms < ?", (lo_ms, hi_ms), limit)


def day_segments(
    db_path: str | Path, day: date, zone: tzinfo, limit: int
) -> list[dict]:
    """At most `limit` segments whose start_ms falls on the date in `zone`, in time order."""
    lo, hi = day_bounds(day, zone)
    return _segments(
        db_path, "end_ms > ? AND start_ms >= ? AND start_ms < ?", (lo, lo, hi), limit
    )


def limitless(db_path: str | Path, day: date, zone: tzinfo) -> list[dict]:
    lo, hi = day_bounds(day, zone)
    conn = store.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT lifelog_id, title, start_ms, end_ms, starred, markdown"
            " FROM limitless_lifelogs WHERE start_ms >= ? AND start_ms < ?"
            " ORDER BY start_ms",
            (lo, hi),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "lifelog_id": r[0],
            "title": r[1],
            "start_ms": r[2],
            "end_ms": r[3],
            "starred": bool(r[4]),
            "markdown": r[5],
        }
        for r in rows
    ]


def pendant_records(
    db_path: str | Path,
    kind: str | None,
    lo_ms: int | None,
    hi_ms: int | None,
    limit: int,
) -> list[dict]:
    """Pendant records with lo_ms <= ts_ms <= hi_ms, by ts_ms. A record with no timestamp (a CRC-failed
    page has none) matches only when no time bound is given, and sorts last."""
    clauses, args = ["1"], list[str | int]()
    if kind is not None:
        clauses.append("kind = ?")
        args.append(kind)
    if lo_ms is not None:
        clauses.append("ts_ms >= ?")
        args.append(lo_ms)
    if hi_ms is not None:
        clauses.append("ts_ms <= ?")
        args.append(hi_ms)
    conn = store.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT kind, ts_ms, session, page_index, data FROM pendant_records"
            f" WHERE {' AND '.join(clauses)}"
            " ORDER BY ts_ms IS NULL, ts_ms, session, page_index, n LIMIT ?",
            (*args, limit),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "kind": r[0],
            "ts_ms": r[1],
            "session": r[2],
            "page_index": r[3],
            "data": json.loads(r[4]),
        }
        for r in rows
    ]


def alive(db_path: str | Path) -> None:
    """Raise unless the store opens and holds its schema, reading no row and counting nothing."""
    conn = store.connect(db_path)
    try:
        for table in ("pages", "limitless_lifelogs", "limitless_audio"):
            conn.execute(f"SELECT 1 FROM {table} LIMIT 0")
    finally:
        conn.close()


def health(db_path: str | Path, worker: dict | None = None) -> dict:
    conn = store.connect(db_path)
    try:
        pages, untranscribed, last_ts = conn.execute(
            "SELECT count(*), count(*) FILTER (WHERE transcribed_at IS NULL),"
            " max(page_ts_ms) FROM pages"
        ).fetchone()
        lifelogs = conn.execute("SELECT count(*) FROM limitless_lifelogs").fetchone()[0]
        audio, audio_left = conn.execute(
            "SELECT count(*), count(*) FILTER (WHERE transcribed_at IS NULL)"
            " FROM limitless_audio"
        ).fetchone()
    finally:
        conn.close()
    return {
        "pages": pages,
        "untranscribed_pages": untranscribed,
        "last_page_ts_ms": last_ts,
        # "stalled" means the worker gave up and the untranscribed pages are not waiting, they are stuck.
        "worker": worker or {"state": "absent", "failures": 0, "error": None},
        "import": {
            "lifelogs": lifelogs,
            "audio": audio,
            "untranscribed_audio": audio_left,
        },
    }
