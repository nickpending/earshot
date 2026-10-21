"""`earshot import-limitless` against a local fake of /v1/lifelogs and /v1/download-audio (L1-L4, L6)."""

import io
import json
import os
import signal
import sqlite3
import struct
import subprocess
import sys
import time
from itertools import pairwise
from pathlib import Path

import av
import pytest
from fake_limitless import (
    HOUR_MS,
    KEY,
    MEETING_MS,
    MEETING_TITLE,
    lifelog,
    standard_archive,
)

from earshotd import cli, importer, limitless, store
from earshotd.config import Config

CHUNKS = 26  # 1 meeting + 3 for the 5 hour lifelog + 22 ordinary


def make_client(fake, key=KEY, **kw):
    return limitless.Client(key, fake.base_url, sleep=lambda _s: None, **kw)


def run_import(config, fake, **kw) -> importer.Report:
    store.init(config.db_path)
    return importer.run(config, make_client(fake, **kw), out=lambda *_a: None)


def rows(config: Config, sql: str, args=()) -> list[tuple]:
    conn = sqlite3.connect(config.db_path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def test_l1_every_lifelog_is_stored_with_its_original_text(limitless_config, make_fake):
    fake = make_fake(standard_archive())
    report = run_import(limitless_config, fake)
    assert (report.listed, report.stored, report.ok) == (24, 24, True)
    assert rows(limitless_config, "SELECT count(*) FROM limitless_lifelogs") == [(24,)]
    row = rows(
        limitless_config,
        "SELECT title, start_ms, end_ms, starred, markdown, contents_json"
        " FROM limitless_lifelogs WHERE lifelog_id = 'll00000'",
    )[0]
    title, start, end, starred, markdown, contents = row
    assert title == MEETING_TITLE
    assert (start, end) == (MEETING_MS, MEETING_MS + 45 * 60_000)
    assert starred == 1
    assert markdown.startswith("# Team meeting") and "line one of 0" in markdown
    parsed = json.loads(contents)
    assert [c["type"] for c in parsed] == ["heading1", "blockquote"]
    assert rows(
        limitless_config,
        "SELECT count(*) FROM limitless_lifelogs WHERE title IS NULL OR start_ms IS NULL"
        " OR end_ms IS NULL OR starred IS NULL OR markdown IS NULL OR contents_json IS NULL",
    ) == [(0,)]


def opus_head(path: str) -> tuple[str, int, int]:
    """codec, channels, and the input sample rate recorded in the OpusHead header."""
    with av.open(path) as c:
        ctx = c.streams.audio[0].codec_context
        head = bytes(ctx.extradata or b"")
        assert head[:8] == b"OpusHead"
        n = sum(1 for _ in c.decode(audio=0))
        assert n > 0
        return ctx.name, head[9], struct.unpack_from("<I", head, 12)[0]


def test_l2_audio_covers_every_lifelog_in_chunks_of_at_most_two_hours(
    limitless_config, make_fake
):
    fake = make_fake(standard_archive())
    run_import(limitless_config, fake)
    lifelogs = rows(
        limitless_config,
        "SELECT lifelog_id, start_ms, end_ms FROM limitless_lifelogs ORDER BY start_ms",
    )
    for lid, start, end in lifelogs:
        chunks = rows(
            limitless_config,
            "SELECT start_ms, end_ms FROM limitless_audio WHERE lifelog_id = ?"
            " ORDER BY start_ms",
            (lid,),
        )
        assert chunks and chunks[0][0] == start and chunks[-1][1] == end, lid
        assert all(b[0] == a[1] for a, b in pairwise(chunks)), lid
        assert all(0 < e - s <= 2 * HOUR_MS for s, e in chunks), lid
    long_drive = rows(
        limitless_config,
        "SELECT start_ms, end_ms FROM limitless_audio WHERE lifelog_id = 'll00001'"
        " ORDER BY start_ms",
    )
    assert [e - s for s, e in long_drive] == [2 * HOUR_MS, 2 * HOUR_MS, HOUR_MS]
    paths = [r[0] for r in rows(limitless_config, "SELECT path FROM limitless_audio")]
    assert len(paths) == CHUNKS
    for p in paths:
        assert Path(p).parent == limitless_config.data_dir / "limitless"
        assert opus_head(p) == ("opus", 1, 16000)
    assert sorted(os.listdir(limitless_config.data_dir / "limitless")) == sorted(
        Path(p).name for p in paths
    )  # no .part file is left behind


def test_l2_every_file_is_fsynced_before_its_row_is_committed(
    limitless_config, make_fake, monkeypatch
):
    seen: list[tuple[set[str], set[str]]] = []
    real = os.fsync

    def spy(fd):
        d = limitless_config.data_dir
        files = (
            set(os.listdir(d / "limitless")) if (d / "limitless").exists() else set()
        )
        committed = {
            Path(r[0]).name
            for r in rows(limitless_config, "SELECT path FROM limitless_audio")
        }
        seen.append((files, committed))
        return real(fd)

    store.init(limitless_config.db_path)
    fake = make_fake(standard_archive(extra=2))
    monkeypatch.setattr(os, "fsync", spy)
    run_import(limitless_config, fake)
    monkeypatch.undo()
    names = [
        Path(r[0]).name
        for r in rows(limitless_config, "SELECT path FROM limitless_audio")
    ]
    assert len(names) == 6
    for name in names:
        # The file's data is synced while it still has its temporary name and no row exists...
        assert any(
            name + ".part" in files and name not in files and name not in committed
            for files, committed in seen
        ), f"{name} data was not fsynced before it was renamed and committed"
        # ...and the directory entry is synced after the rename, still before the row.
        assert any(
            name in files and name not in committed for files, committed in seen
        ), f"{name} directory was not fsynced before its row committed"


def test_l2_audio_that_is_not_opus_16k_mono_is_refused_and_reported(
    limitless_config, make_fake
):
    fake = make_fake(standard_archive(extra=0), audio_rate=48000)
    report = run_import(limitless_config, fake)
    assert not report.ok
    assert sorted(report.failures) == ["ll00000", "ll00001"]
    assert rows(limitless_config, "SELECT count(*) FROM limitless_audio") == [(0,)]
    assert list((limitless_config.data_dir / "limitless").glob("*")) == []


def test_l3_a_second_run_downloads_nothing_and_changes_nothing(
    limitless_config, make_fake
):
    fake = make_fake(standard_archive())
    run_import(limitless_config, fake)
    snapshot = rows(limitless_config, "SELECT * FROM limitless_audio ORDER BY 1, 2, 3")
    first = len(fake.audio_requests())
    assert first == CHUNKS
    report = run_import(limitless_config, fake)
    assert (
        report.ok and report.chunks_downloaded == 0 and report.chunks_skipped == CHUNKS
    )
    assert len(fake.audio_requests()) == first
    assert (
        rows(limitless_config, "SELECT * FROM limitless_audio ORDER BY 1, 2, 3")
        == snapshot
    )


def test_l3_a_committed_row_whose_file_is_gone_is_downloaded_again(
    limitless_config, make_fake
):
    fake = make_fake(standard_archive(extra=0))
    run_import(limitless_config, fake)
    victim = rows(limitless_config, "SELECT path FROM limitless_audio ORDER BY 1")[0][0]
    os.unlink(victim)
    report = run_import(limitless_config, fake)
    assert report.ok and report.chunks_downloaded == 1
    assert Path(victim).exists()
    assert rows(limitless_config, "SELECT count(*) FROM limitless_audio") == [(4,)]


def _write_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "earshotd.toml"
    cfg.write_text(f'data_dir = "{tmp_path / "data"}"\n')
    cfg.chmod(0o600)  # config.load refuses a config others can read
    return cfg


def _spawn(cfg: Path, fake) -> subprocess.Popen:
    p = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "earshotd.cli",
            "--config",
            str(cfg),
            "import-limitless",
            "--api-base",
            fake.base_url,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert p.stdin is not None
    p.stdin.write(KEY + "\n")  # the real non-terminal path: the key arrives on stdin
    p.stdin.flush()  # left open: communicate() closes it
    return p


def _db(tmp_path: Path):
    return sqlite3.connect(tmp_path / "data" / "earshot.db")


def test_l3_sigkill_partway_then_rerun_ends_with_the_uninterrupted_counts(
    tmp_path, make_fake
):
    archive = standard_archive()
    fake = make_fake(archive, audio_delay_s=0.2)
    cfg = _write_config(tmp_path)
    p = _spawn(cfg, fake)
    deadline = time.time() + 60
    try:
        while len(fake.audio_requests()) < 6 and time.time() < deadline:
            time.sleep(0.02)
        assert len(fake.audio_requests()) >= 6, (
            "the import never reached its audio downloads"
        )
        os.kill(p.pid, signal.SIGKILL)
        p.wait()
    finally:
        if p.poll() is None:
            p.kill()
    assert p.returncode == -signal.SIGKILL
    conn = _db(tmp_path)
    committed = set(conn.execute("SELECT start_ms, end_ms FROM limitless_audio"))
    conn.close()
    assert 0 < len(committed) < CHUNKS
    requested_before = len(fake.audio_requests())

    fake.audio_delay_s = 0.0
    second = _spawn(cfg, fake)
    out, err = second.communicate(timeout=120)
    assert second.returncode == 0, err
    again = set(fake.audio_requests()[requested_before:])
    assert not (again & committed), "a committed chunk was downloaded again"
    assert len(again) == CHUNKS - len(committed)

    conn = _db(tmp_path)
    try:
        assert conn.execute("SELECT count(*) FROM limitless_lifelogs").fetchone() == (
            24,
        )
        assert conn.execute("SELECT count(*) FROM limitless_audio").fetchone() == (
            CHUNKS,
        )
        assert conn.execute(
            "SELECT count(*) FROM (SELECT 1 FROM limitless_audio"
            " GROUP BY lifelog_id, start_ms, end_ms HAVING count(*) > 1)"
        ).fetchone() == (0,)
    finally:
        conn.close()
    files = sorted(os.listdir(tmp_path / "data" / "limitless"))
    assert len(files) == CHUNKS and not [f for f in files if f.endswith(".part")]
    assert "listed 24" in out and "stored 24" in out
    assert fake.keys and set(fake.keys) == {KEY}
    assert KEY not in out + err


def test_l4_429_and_5xx_are_retried_and_the_import_completes(
    limitless_config, make_fake
):
    fake = make_fake(standard_archive(extra=2))
    fake.list_faults = [429, 502]
    first = (MEETING_MS, MEETING_MS + 45 * 60_000)
    fake.audio_faults[first[0]] = [500, 429, 503]
    report = run_import(limitless_config, fake)
    assert report.ok and report.failures == {}
    assert rows(limitless_config, "SELECT count(*) FROM limitless_audio") == [(6,)]
    assert fake.audio_requests().count(first) == 4


def test_l4_what_still_fails_is_listed_by_lifelog_id_and_the_exit_is_non_zero(
    limitless_config, make_fake, capsys
):
    archive = standard_archive(extra=3)
    fake = make_fake(archive)
    long_drive_start = MEETING_MS + 2 * HOUR_MS
    fake.always_fail_audio[long_drive_start] = 503
    fake.always_fail_audio[MEETING_MS + 24 * HOUR_MS] = 429  # ll00002
    store.init(limitless_config.db_path)
    code = cli.main(
        ["import-limitless", "--api-base", fake.base_url],
        config=limitless_config,
        limitless_sleep=lambda _s: None,
        read_key=lambda: KEY,
    )
    out = capsys.readouterr()
    assert code == 1
    assert "ll00001" in out.out + out.err and "ll00002" in out.out + out.err
    assert KEY not in out.out + out.err
    # everything else was still imported
    assert rows(limitless_config, "SELECT count(*) FROM limitless_lifelogs") == [(5,)]
    ok_ids = {
        r[0]
        for r in rows(
            limitless_config, "SELECT DISTINCT lifelog_id FROM limitless_audio"
        )
    }
    assert ok_ids == {"ll00000", "ll00003", "ll00004"}
    # a later run finishes the job once the API behaves
    fake.always_fail_audio.clear()
    store.init(limitless_config.db_path)
    code = cli.main(
        ["import-limitless", "--api-base", fake.base_url],
        config=limitless_config,
        limitless_sleep=lambda _s: None,
        read_key=lambda: KEY,
    )
    assert code == 0
    assert rows(limitless_config, "SELECT count(*) FROM limitless_audio") == [(7,)]


def test_a_403_on_the_listing_exits_non_zero_and_stores_nothing(
    limitless_config, make_fake, capsys
):
    fake = make_fake(standard_archive())
    store.init(limitless_config.db_path)
    # Reproduce Cloudflare's answer to the default urllib agent through the client the CLI builds.
    real = limitless.Client.__init__

    def init(self, *a, **k):
        real(self, *a, **k)
        self.user_agent = "Python-urllib/3.12"

    limitless.Client.__init__ = init  # type: ignore[method-assign]
    try:
        code = cli.main(
            ["import-limitless", "--api-base", fake.base_url],
            config=limitless_config,
            limitless_sleep=lambda _s: None,
            read_key=lambda: KEY,
        )
    finally:
        limitless.Client.__init__ = real  # type: ignore[method-assign]
    err = capsys.readouterr().err
    assert code == 1 and "403" in err
    assert rows(limitless_config, "SELECT count(*) FROM limitless_lifelogs") == [(0,)]


def test_an_archive_that_lists_empty_is_not_a_success(limitless_config, make_fake):
    fake = make_fake([])
    report = run_import(limitless_config, fake)
    assert not report.ok


def test_a_lifelog_with_no_time_range_is_stored_and_named_in_the_report(
    limitless_config, make_fake
):
    fake = make_fake(
        [
            lifelog(0, MEETING_MS, MEETING_MS),
            lifelog(1, MEETING_MS, MEETING_MS + 60_000),
        ]
    )
    report = run_import(limitless_config, fake)
    assert report.stored == 2 and report.no_audio == ["ll00000"]
    assert rows(limitless_config, "SELECT lifelog_id FROM limitless_audio") == [
        ("ll00001",)
    ]


def test_the_summary_prints_the_api_count_next_to_the_stored_count(
    limitless_config, make_fake, capsys
):
    fake = make_fake(standard_archive())
    store.init(limitless_config.db_path)
    code = cli.main(
        ["import-limitless", "--api-base", fake.base_url],
        config=limitless_config,
        limitless_sleep=lambda _s: None,
        read_key=lambda: KEY,
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "listed 24" in out and "stored 24" in out


def test_c3_a_refused_api_base_fails_the_import_without_the_key(
    limitless_config, make_fake, capsys
):
    fake = make_fake(standard_archive(extra=0))
    code = cli.main(
        ["import-limitless", "--api-base", "http://example.com"],
        config=limitless_config,
        limitless_sleep=lambda _s: None,
        read_key=lambda: KEY,
    )
    err = capsys.readouterr().err
    assert code == 1
    assert "import failed" in err and "http" in err and "example.com" in err
    assert KEY not in err
    assert fake.requests == []


def test_c7_the_key_goes_on_every_request_and_into_no_output(
    limitless_config, make_fake, capsys
):
    fake = make_fake(standard_archive(extra=1))
    code = cli.main(
        ["import-limitless", "--api-base", fake.base_url],
        config=limitless_config,
        limitless_sleep=lambda _s: None,
        read_key=lambda: KEY,
    )
    out = capsys.readouterr()
    assert code == 0
    assert fake.keys and set(fake.keys) == {KEY}
    assert KEY not in out.out + out.err and KEY[:8] not in out.out + out.err


def test_c7_an_empty_key_is_refused_before_any_request(
    limitless_config, make_fake, capsys
):
    fake = make_fake(standard_archive(extra=0))
    code = cli.main(
        ["import-limitless", "--api-base", fake.base_url],
        config=limitless_config,
        read_key=lambda: "",
    )
    assert code == 1
    assert "no Limitless API key was given" in capsys.readouterr().err
    assert fake.requests == []


class _Tty:
    def isatty(self) -> bool:
        return True

    def readline(self) -> str:
        raise AssertionError("the hidden prompt must not read stdin")


def test_c7_on_a_terminal_the_key_comes_from_the_hidden_prompt(monkeypatch):
    prompts: list[str] = []

    def fake_getpass(prompt: str) -> str:
        prompts.append(prompt)
        return KEY

    monkeypatch.setattr(sys, "stdin", _Tty())
    monkeypatch.setattr(cli.getpass, "getpass", fake_getpass)
    assert cli._read_key() == KEY
    assert len(prompts) == 1


def test_c7_on_a_pipe_the_key_is_the_first_line_without_its_newline(monkeypatch):
    def no_prompt(*_a):
        raise AssertionError("no prompt on a pipe")

    monkeypatch.setattr(sys, "stdin", io.StringIO(KEY + "\nmore\n"))
    monkeypatch.setattr(cli.getpass, "getpass", no_prompt)
    assert cli._read_key() == KEY


def _lifelogs_with_ids(ids: list[str]) -> list[dict]:
    out = []
    for n, lid in enumerate(ids):
        start = MEETING_MS + n * HOUR_MS
        out.append({**lifelog(n, start, start + 20 * 60_000), "id": lid})
    return out


ESCAPE_DIGEST = "efbf103bcec54b370d5fdbcd97c853944c0e6bf61a446c27f2552c06847c5df6"


def test_o11_the_audio_pass_holds_no_lifelog_text(
    limitless_config, make_fake, monkeypatch
):
    fake = make_fake(standard_archive(extra=2))
    seen: list[object] = []
    real = importer._import_audio

    def spy(config, client, lifelog, report):
        seen.append(lifelog)
        return real(config, client, lifelog, report)

    monkeypatch.setattr(importer, "_import_audio", spy)
    report = run_import(limitless_config, fake)
    assert report.ok and len(seen) == 4
    for item in seen:
        assert not hasattr(item, "markdown") and not hasattr(item, "contents_json")
    assert rows(limitless_config, "SELECT count(*) FROM limitless_audio") == [(6,)]
    again = run_import(limitless_config, fake)  # a settled store: nothing downloaded
    assert again.ok and again.chunks_downloaded == 0 and again.chunks_skipped == 6


def test_c4_no_lifelog_id_can_steer_the_chunk_file_path(limitless_config, make_fake):
    fake = make_fake(_lifelogs_with_ids(["../../escape", "a/b", "ll00002"]))
    report = run_import(limitless_config, fake)
    assert report.ok
    folder = limitless_config.data_dir / "limitless"
    found = rows(
        limitless_config,
        "SELECT lifelog_id, start_ms, end_ms, path FROM limitless_audio ORDER BY 1",
    )
    assert len(found) == 3
    for _lid, _s, _e, path in found:
        assert Path(path).parent == folder
    by_id = {r[0]: r for r in found}
    _lid, start, end, path = by_id["../../escape"]
    assert Path(path).name == f"{ESCAPE_DIGEST}_{start}_{end}.ogg"
    assert not [n for n in os.listdir(folder) if "escape" in n]
    assert not list(limitless_config.data_dir.glob("escape_*"))
    assert not list(limitless_config.data_dir.parent.glob("escape_*"))


def test_c5_rows_with_old_id_named_paths_are_still_found_and_unchanged(
    limitless_config, make_fake
):
    fake = make_fake(standard_archive(extra=2))
    run_import(limitless_config, fake)
    conn = sqlite3.connect(limitless_config.db_path)
    for lid, start, end, path in conn.execute(
        "SELECT lifelog_id, start_ms, end_ms, path FROM limitless_audio"
    ).fetchall():
        old = Path(path).with_name(f"{lid}_{start}_{end}.ogg")
        os.rename(path, old)
        conn.execute(
            "UPDATE limitless_audio SET path = ? WHERE lifelog_id = ? AND start_ms = ?"
            " AND end_ms = ?",
            (str(old), lid, start, end),
        )
    conn.commit()
    conn.close()
    snapshot = rows(limitless_config, "SELECT * FROM limitless_audio ORDER BY 1, 2, 3")
    before = len(fake.audio_requests())
    code = cli.main(
        ["import-limitless", "--api-base", fake.base_url],
        config=limitless_config,
        limitless_sleep=lambda _s: None,
        read_key=lambda: KEY,
    )
    assert code == 0
    assert len(fake.audio_requests()) == before
    assert (
        rows(limitless_config, "SELECT * FROM limitless_audio ORDER BY 1, 2, 3")
        == snapshot
    )
    report = run_import(limitless_config, fake)
    assert report.chunks_downloaded == 0 and report.chunks_skipped == 6


def test_l6_only_the_two_endpoints_are_called_and_the_key_never_comes_from_the_environment(
    limitless_config, make_fake
):
    fake = make_fake(standard_archive(extra=1))
    run_import(limitless_config, fake)
    assert fake.paths() == {"/v1/lifelogs", "/v1/download-audio"}
    src = Path(importer.__file__).parent
    code = "".join(
        (src / n).read_text() for n in ("limitless.py", "importer.py", "cli.py")
    )
    assert "/v1/chats" not in code
    assert "environ" not in (src / "limitless.py").read_text()
    assert "environ" not in (src / "importer.py").read_text()
    assert "sqlite3" not in (src / "limitless.py").read_text()
    assert "sqlite3" not in (src / "importer.py").read_text()
    assert "api.limitless.ai" in code


def test_the_audio_check_reads_what_the_file_says(limitless_config, make_fake):
    from fake_limitless import ogg_opus

    assert importer.check_audio(ogg_opus(1.0)) is None
    assert "16000" in (importer.check_audio(ogg_opus(1.0, rate=48000)) or "")
    assert "channel" in (importer.check_audio(ogg_opus(1.0, channels=2)) or "")
    assert importer.check_audio(b"<html>nope</html>")
    assert importer.check_audio(b"")


NO_AUDIO_TEXT = "No audio available for the requested time range"


def test_no_audio_chunk_is_stored_reported_on_its_own_line_and_exits_zero(
    limitless_config, make_fake, capsys
):
    fake = make_fake(standard_archive(extra=3))
    fake.no_audio_at.add(MEETING_MS + 24 * HOUR_MS)  # ll00002
    store.init(limitless_config.db_path)
    code = cli.main(
        ["import-limitless", "--api-base", fake.base_url],
        config=limitless_config,
        limitless_sleep=lambda _s: None,
        read_key=lambda: KEY,
    )
    out = capsys.readouterr()
    assert code == 0
    start = MEETING_MS + 24 * HOUR_MS
    row = rows(
        limitless_config,
        "SELECT lifelog_id, start_ms, end_ms, error, checked_at"
        " FROM limitless_audio_absent",
    )
    assert len(row) == 1
    assert row[0][:4] == ("ll00002", start, start + 20 * 60_000, NO_AUDIO_TEXT)
    assert abs(row[0][4] - time.time() * 1000) < 60_000
    assert "no audio at Limitless, 1: ll00002" in out.out + out.err
    assert "FAILED" not in out.out + out.err
    assert rows(limitless_config, "SELECT count(*) FROM limitless_audio") == [(6,)]


def test_a_stored_no_audio_chunk_is_never_requested_again(limitless_config, make_fake):
    fake = make_fake(standard_archive(extra=3))
    start = MEETING_MS + 24 * HOUR_MS
    fake.no_audio_at.add(start)
    first = run_import(limitless_config, fake)
    assert first.ok and first.no_audio_at_limitless == ["ll00002"]
    before = len(fake.audio_requests())
    second = run_import(limitless_config, fake)
    assert second.ok and second.chunks_downloaded == 0
    assert second.chunks_skipped == 7  # 6 stored chunks + the settled no-audio one
    assert len(fake.audio_requests()) == before
    assert (start, start + 20 * 60_000) not in fake.audio_requests()[before:]
    assert rows(limitless_config, "SELECT count(*) FROM limitless_audio_absent") == [
        (1,)
    ]


def test_no_audio_on_one_chunk_does_not_stop_the_lifelogs_other_chunks(
    limitless_config, make_fake
):
    fake = make_fake(standard_archive(extra=0))
    fake.no_audio_at.add(MEETING_MS + 2 * HOUR_MS + 2 * HOUR_MS)  # 2nd chunk of ll00001
    report = run_import(limitless_config, fake)
    assert report.ok and report.no_audio_at_limitless == ["ll00001"]
    assert rows(limitless_config, "SELECT count(*) FROM limitless_audio") == [(3,)]
    assert rows(limitless_config, "SELECT count(*) FROM limitless_audio_absent") == [
        (1,)
    ]


@pytest.mark.parametrize(
    "status, body",
    [
        (404, {"error": "not found"}),
        (404, {"error": NO_AUDIO_TEXT + "."}),
        (400, {"error": NO_AUDIO_TEXT}),
        (401, {"error": NO_AUDIO_TEXT}),
        (403, {"error": NO_AUDIO_TEXT}),
    ],
)
def test_any_other_4xx_is_still_a_failure_and_exits_one(
    limitless_config, make_fake, capsys, status, body
):
    fake = make_fake(standard_archive(extra=1))
    fake.audio_errors[MEETING_MS + 24 * HOUR_MS] = (status, body)
    store.init(limitless_config.db_path)
    code = cli.main(
        ["import-limitless", "--api-base", fake.base_url],
        config=limitless_config,
        limitless_sleep=lambda _s: None,
        read_key=lambda: KEY,
    )
    out = capsys.readouterr()
    assert code == 1
    assert "FAILED" in out.out + out.err and "ll00002" in out.out + out.err
    assert rows(limitless_config, "SELECT count(*) FROM limitless_audio_absent") == [
        (0,)
    ]


def test_the_no_audio_table_is_added_beside_existing_rows(limitless_config, make_fake):
    fake = make_fake(standard_archive(extra=1))
    run_import(limitless_config, fake)
    conn = sqlite3.connect(limitless_config.db_path)
    conn.execute("DROP TABLE limitless_audio_absent")
    conn.commit()
    conn.close()
    snapshot = rows(limitless_config, "SELECT * FROM limitless_audio ORDER BY 1, 2, 3")
    lifelogs = rows(limitless_config, "SELECT * FROM limitless_lifelogs ORDER BY 1")
    store.init(limitless_config.db_path)  # earshotd starting on the existing store
    assert rows(limitless_config, "SELECT count(*) FROM limitless_audio_absent") == [
        (0,)
    ]
    assert (
        rows(limitless_config, "SELECT * FROM limitless_audio ORDER BY 1, 2, 3")
        == snapshot
    )
    assert (
        rows(limitless_config, "SELECT * FROM limitless_lifelogs ORDER BY 1")
        == lifelogs
    )
