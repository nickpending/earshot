import os
import shutil
import sqlite3
import zoneinfo
from pathlib import Path

import model_paths
import pytest
from fake_limitless import FakeLimitless
from fastapi.testclient import TestClient

from earshotd.app import create_app
from earshotd.config import Config

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "say-test.pages.v1"
CT = "application/vnd.earshot.pages.v1"
UP, RD = "a" * 64, "b" * 64
T0 = 1790896082170  # 2026-10-01 23:08:02 UTC, 2026-10-02 08:08:02 in Asia/Tokyo


def tzfile(tmp_path: Path, name: str) -> Path:
    """A copy of tzdata's file for the zone `name`, the form of a host's /etc/localtime."""
    for root in zoneinfo.TZPATH:
        src = Path(root) / name
        if src.is_file():
            dst = tmp_path / ("localtime-" + name.replace("/", "-"))
            shutil.copy(src, dst)
            return dst
    raise FileNotFoundError(f"no tzdata file for {name} under {zoneinfo.TZPATH}")


_verdicts: dict[str, model_paths.State] = {}


def pytest_sessionstart(session):
    """Judge each model once, before any test runs, so no test can change a verdict."""
    _verdicts["diarization"] = model_paths.diarization_state()
    _verdicts["whisper"] = model_paths.whisper_state()


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    """Skip a model-marked test when its model is absent and fail it when broken, before any fixture."""
    states = []
    for marker in item.iter_markers("model"):
        for name in marker.args:
            state = _verdicts.get(name)
            if state is None:
                pytest.fail(
                    f"model marker names {name!r}, not a known model", pytrace=False
                )
            states.append(state)
    for state in states:
        if state.status == "broken":
            pytest.fail(state.reason, pytrace=False)
    for state in states:
        if state.status == "absent":
            pytest.skip(state.reason)


@pytest.fixture(autouse=True)
def _restore_umask():
    """app.main and cli.main set the process umask; no test may leak it into the next."""
    before = os.umask(0o022)
    os.umask(before)
    yield
    os.umask(before)


@pytest.fixture
def body() -> bytes:
    return FIXTURE.read_bytes()


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(tokens={UP: "upload", RD: "read"}, data_dir=tmp_path)


@pytest.fixture
def client(config) -> TestClient:
    return TestClient(create_app(config))


@pytest.fixture
def make_fake():
    """Start fakes of the Limitless API; each is stopped when the test ends."""
    started: list[FakeLimitless] = []

    def make(lifelogs, **kw) -> FakeLimitless:
        fake = FakeLimitless(lifelogs, **kw)
        fake.start()
        started.append(fake)
        return fake

    yield make
    for fake in started:
        fake.stop()


@pytest.fixture
def limitless_config(tmp_path) -> Config:
    return Config(tokens={UP: "upload", RD: "read"}, data_dir=tmp_path)


def page_count(config: Config) -> int:
    conn = sqlite3.connect(config.db_path)
    try:
        return conn.execute("SELECT count(*) FROM pages").fetchone()[0]
    finally:
        conn.close()


def up(token=UP, ctype=CT):
    h = {"Content-Type": ctype}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h
