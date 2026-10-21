"""SQLite store: the single source of truth. A write is durable before it returns."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path
from typing import NamedTuple

from . import records
from .pages import Page

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    session INTEGER NOT NULL,
    page_index INTEGER NOT NULL,
    page_ts_ms INTEGER NOT NULL,
    frames BLOB NOT NULL,
    transcribed_at INTEGER,
    PRIMARY KEY (session, page_index, page_ts_ms)
);
CREATE INDEX IF NOT EXISTS pages_ts ON pages (page_ts_ms);
CREATE TABLE IF NOT EXISTS raw_pages (
    session INTEGER NOT NULL,
    page_index INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    raw BLOB NOT NULL,
    PRIMARY KEY (session, page_index, sha256)
);
CREATE TABLE IF NOT EXISTS pendant_records (
    session INTEGER NOT NULL,
    page_index INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    n INTEGER NOT NULL,
    kind TEXT NOT NULL,
    ts_ms INTEGER,
    data TEXT NOT NULL,
    PRIMARY KEY (session, page_index, sha256, n)
);
CREATE INDEX IF NOT EXISTS pendant_records_kind_ts ON pendant_records (kind, ts_ms);
CREATE TABLE IF NOT EXISTS limitless_lifelogs (
    lifelog_id TEXT PRIMARY KEY,
    title TEXT,
    start_ms INTEGER,
    end_ms INTEGER,
    starred INTEGER,
    markdown TEXT,
    contents_json TEXT
);
CREATE TABLE IF NOT EXISTS limitless_audio (
    lifelog_id TEXT,
    start_ms INTEGER,
    end_ms INTEGER,
    path TEXT,
    transcribed_at INTEGER,
    done_ms INTEGER
);
CREATE TABLE IF NOT EXISTS limitless_audio_absent (
    lifelog_id TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    error TEXT NOT NULL,
    checked_at INTEGER NOT NULL,
    PRIMARY KEY (lifelog_id, start_ms, end_ms)
);
CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    text TEXT NOT NULL,
    source TEXT NOT NULL,
    speaker TEXT,
    name TEXT,
    model TEXT
);
CREATE INDEX IF NOT EXISTS segments_start ON segments (start_ms);
CREATE TABLE IF NOT EXISTS day_speakers (
    date TEXT NOT NULL,
    speaker TEXT NOT NULL,
    centroid BLOB,
    PRIMARY KEY (date, speaker)
);
CREATE TABLE IF NOT EXISTS voices (
    name TEXT PRIMARY KEY,
    embedding BLOB
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    # isolation_level=None: transactions are explicit (BEGIN IMMEDIATE ... COMMIT).
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def init(db_path: str | Path) -> None:
    Path(db_path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Created private here, not by SQLite: it gives -wal and -shm the main file's mode.
    os.close(os.open(db_path, os.O_WRONLY | os.O_CREAT, 0o600))
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring a store created by an earlier version up to the current schema."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(limitless_audio)")}
    if "done_ms" not in cols:
        conn.execute("ALTER TABLE limitless_audio ADD COLUMN done_ms INTEGER")
    # One row per chunk: a re-run can never duplicate one.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS limitless_audio_chunk"
        " ON limitless_audio (lifelog_id, start_ms, end_ms)"
    )


def store_pages(db_path: str | Path, pages: list[Page]) -> tuple[int, int]:
    """Insert pages in one transaction; return (stored, duplicates) only after COMMIT."""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            stored = 0
            for p in pages:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO pages (session, page_index, page_ts_ms, frames)"
                    " VALUES (?, ?, ?, ?)",
                    (p.session, p.page_index, p.page_ts_ms, p.frames),
                )
                stored += cur.rowcount
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return stored, len(pages) - stored
    finally:
        conn.close()


def _write_decoded(conn: sqlite3.Connection, sha: str, d: records.Decoded) -> None:
    if d.audio is not None:
        p = d.audio
        conn.execute(
            "INSERT OR IGNORE INTO pages (session, page_index, page_ts_ms, frames)"
            " VALUES (?, ?, ?, ?)",
            (p.session, p.page_index, p.page_ts_ms, p.frames),
        )
    conn.executemany(
        "INSERT OR REPLACE INTO pendant_records"
        " (session, page_index, sha256, n, kind, ts_ms, data) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (d.session, d.page_index, sha, i, r.kind, r.ts_ms, r.data_json())
            for i, r in enumerate(d.records)
        ],
    )


def store_pages_v2(db_path: str | Path, raws: list[bytes]) -> tuple[int, int]:
    """Store raw StorageBuffers, their audio pages and records in one transaction.

    Returns (stored, duplicates) only after COMMIT. Raises records.DecodeError, with nothing
    written, when any buffer does not decode.
    """
    decoded = [records.decode(raw) for raw in raws]
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            stored = 0
            for raw, d in zip(raws, decoded):
                sha = hashlib.sha256(raw).hexdigest()
                cur = conn.execute(
                    "INSERT OR IGNORE INTO raw_pages (session, page_index, sha256, raw)"
                    " VALUES (?, ?, ?, ?)",
                    (d.session, d.page_index, sha, raw),
                )
                stored += cur.rowcount
                _write_decoded(conn, sha, d)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return stored, len(raws) - stored
    finally:
        conn.close()


MAX_NAMED_FAILURES = 100  # redecode names this many failed raw pages and counts the rest


def redecode(db_path: str | Path) -> tuple[int, list[str]]:
    """Rebuild pendant_records and the audio pages rows from raw_pages in one transaction.

    Existing pages rows (and their transcribed_at) are kept; missing ones are inserted.
    Reads one raw page at a time. Returns (pages decoded, one message per raw page that no longer
    decodes): the first MAX_NAMED_FAILURES are named, and any more are one last message counting
    them.
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM pendant_records")
            count, unnamed = 0, 0
            failed: list[str] = []
            rows = conn.execute(
                "SELECT session, page_index, sha256, raw FROM raw_pages"
                " ORDER BY session, page_index, sha256"
            )
            for session, page_index, sha, raw in rows:
                try:
                    d = records.decode(raw)
                except records.DecodeError as e:
                    if len(failed) < MAX_NAMED_FAILURES:
                        failed.append(f"session {session} page {page_index}: {e}")
                    else:
                        unnamed += 1
                    continue
                _write_decoded(conn, sha, d)
                count += 1
            if unnamed:
                failed.append(f"{unnamed} more raw page{'s' if unnamed > 1 else ''}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return count, failed
    finally:
        conn.close()


def rebuild(db_path: str | Path) -> dict[str, int]:
    """Clear everything derived from audio, in one transaction; return rows cleared per table.

    Segments and day_speakers are emptied and every audio page and imported chunk is marked
    untranscribed again. Voices, audio, raw pages, records and lifelogs are not touched.
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cleared = {
                "segments": conn.execute("DELETE FROM segments").rowcount,
                "day_speakers": conn.execute("DELETE FROM day_speakers").rowcount,
                "pages": conn.execute(
                    "UPDATE pages SET transcribed_at = NULL"
                    " WHERE transcribed_at IS NOT NULL"
                ).rowcount,
                "limitless_audio": conn.execute(
                    "UPDATE limitless_audio SET transcribed_at = NULL, done_ms = NULL"
                    " WHERE transcribed_at IS NOT NULL OR done_ms IS NOT NULL"
                ).rowcount,
            }
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return cleared
    finally:
        conn.close()


class Lifelog(NamedTuple):
    lifelog_id: str
    title: str | None
    start_ms: int
    end_ms: int
    starred: bool
    markdown: str | None
    contents_json: str


def upsert_lifelogs(db_path: str | Path, lifelogs: list[Lifelog]) -> None:
    """Insert or update lifelogs by lifelog_id in one transaction, durable on return."""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany(
                "INSERT INTO limitless_lifelogs (lifelog_id, title, start_ms, end_ms, starred,"
                " markdown, contents_json) VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (lifelog_id) DO UPDATE SET title = excluded.title,"
                " start_ms = excluded.start_ms, end_ms = excluded.end_ms,"
                " starred = excluded.starred, markdown = excluded.markdown,"
                " contents_json = excluded.contents_json",
                [
                    (
                        x.lifelog_id,
                        x.title,
                        x.start_ms,
                        x.end_ms,
                        int(x.starred),
                        x.markdown,
                        x.contents_json,
                    )
                    for x in lifelogs
                ],
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def audio_stored(
    db_path: str | Path, lifelog_id: str, start_ms: int, end_ms: int
) -> bool:
    """Whether this chunk is committed and its file is still on disk."""
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT path FROM limitless_audio"
            " WHERE lifelog_id = ? AND start_ms = ? AND end_ms = ?",
            (lifelog_id, start_ms, end_ms),
        ).fetchone()
    finally:
        conn.close()
    return row is not None and Path(row[0]).is_file()


def audio_absent(
    db_path: str | Path, lifelog_id: str, start_ms: int, end_ms: int
) -> bool:
    """Whether Limitless has said, for good, that it holds no audio for this chunk."""
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM limitless_audio_absent"
            " WHERE lifelog_id = ? AND start_ms = ? AND end_ms = ?",
            (lifelog_id, start_ms, end_ms),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def store_audio_absent(
    db_path: str | Path,
    lifelog_id: str,
    start_ms: int,
    end_ms: int,
    error: str,
    checked_at_ms: int,
) -> None:
    """Record that Limitless answered "no audio" for this chunk; an earlier row is kept."""
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO limitless_audio_absent"
            " (lifelog_id, start_ms, end_ms, error, checked_at) VALUES (?, ?, ?, ?, ?)",
            (lifelog_id, start_ms, end_ms, error, checked_at_ms),
        )
    finally:
        conn.close()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def store_audio(
    db_path: str | Path,
    data_dir: str | Path,
    lifelog_id: str,
    start_ms: int,
    end_ms: int,
    data: bytes,
) -> Path:
    """Write one chunk's Ogg file under `data_dir/limitless`, fsync it, then commit its row.

    The file is written under a temporary name, synced, renamed and the directory synced before
    the row exists, so a committed row always points at a complete, durable file; a crash leaves
    at worst a file with no row, which the next run replaces.
    """
    folder = Path(data_dir) / "limitless"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    # The id is the API's text: hash it so it can never steer the path.
    digest = hashlib.sha256(lifelog_id.encode()).hexdigest()
    final = folder / f"{digest}_{start_ms}_{end_ms}.ogg"
    part = final.with_name(final.name + ".part")
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(part, final)
    _fsync_dir(folder)
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO limitless_audio (lifelog_id, start_ms, end_ms, path)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT (lifelog_id, start_ms, end_ms) DO UPDATE SET path = excluded.path",
            (lifelog_id, start_ms, end_ms, str(final)),
        )
    finally:
        conn.close()
    return final


def count_lifelogs(db_path: str | Path) -> int:
    conn = connect(db_path)
    try:
        return conn.execute("SELECT count(*) FROM limitless_lifelogs").fetchone()[0]
    finally:
        conn.close()
