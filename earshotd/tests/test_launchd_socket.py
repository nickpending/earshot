"""earshotd.app.main as a launchd job (macOS only): the socket launchd binds, and what a bad plist does.

Each test bootstraps a throwaway agent into gui/<uid> under a unique label from a plist
deploy/macos/launchd.py rendered, and boots it out afterwards.
"""

import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest
from launchd_layout import build
from test_launchd_render import DIARIZATION, GOOD, LAUNCHD, WHISPER

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or not os.path.exists("/usr/bin/sandbox-exec"),
    reason="launchd and sandbox-exec are macOS",
)

DOMAIN = f"gui/{os.getuid()}"
VENV = Path(sys.prefix)


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def toml_string(value: object) -> str:
    return json.dumps(str(value))


class Service:
    """A service root laid out as install.sh does, rendered for a throwaway label."""

    def __init__(self, tmp_path: Path, port: int):
        self.tmp_path = tmp_path
        self.label = f"test.earshot.{uuid.uuid4().hex[:12]}"
        self.home, self.root, self.config, self.log = build(tmp_path)
        self.plist = tmp_path / f"{self.label}.plist"
        self.profile = self.root / "earshotd.sb"
        self.port = port
        for model in (WHISPER, DIARIZATION):
            (self.root / "models" / model).mkdir()
        # the test environment's own interpreter and packages, as a venv at <root>/app/.venv
        venv = self.root / "app" / ".venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "lib" / "python3.12").mkdir(parents=True)
        (venv / "bin" / "python").symlink_to(os.path.realpath(sys.executable))
        shutil.copy(VENV / "pyvenv.cfg", venv / "pyvenv.cfg")
        (venv / "lib" / "python3.12" / "site-packages").symlink_to(
            next((VENV / "lib").glob("python3*")) / "site-packages"
        )
        self.write_config(port)

    def write_config(self, port: int) -> None:
        self.config.write_text(
            f'listen = "127.0.0.1:{port}"\n'
            f"data_dir = {toml_string(self.root / 'data')}\n"
            f"whisper_model = {toml_string(self.root / 'models' / WHISPER)}\n"
            f"diarization_model = {toml_string(self.root / 'models' / DIARIZATION)}\n"
            f'[[tokens]]\ntoken = "{GOOD}"\nscope = "read"\n'
        )
        self.config.chmod(0o600)

    def render(self) -> dict:
        done = subprocess.run(
            [
                sys.executable, "-I", str(LAUNCHD), "render", str(self.config), str(self.root),
                str(self.home), str(self.log), self.label, str(self.plist), str(self.profile),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode == 0, done.stderr
        return plistlib.loads(self.plist.read_bytes())

    def edit_plist(self, change) -> None:
        data = plistlib.loads(self.plist.read_bytes())
        change(data)
        self.plist.write_bytes(plistlib.dumps(data))

    def bootstrap(self) -> None:
        subprocess.run(
            ["launchctl", "bootstrap", DOMAIN, str(self.plist)], check=True, capture_output=True
        )

    def bootout(self) -> None:
        subprocess.run(
            ["launchctl", "bootout", f"{DOMAIN}/{self.label}"], capture_output=True, check=False
        )

    def print(self) -> str:
        return subprocess.run(
            ["launchctl", "print", f"{DOMAIN}/{self.label}"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout

    def log_text(self) -> str:
        return self.log.read_text() if self.log.exists() else ""

    def wait_for_log(self, needle: str, seconds: float = 40) -> str:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if needle in self.log_text():
                break
            time.sleep(0.3)
        return self.log_text()


@pytest.fixture
def service(tmp_path):
    made: list[Service] = []

    def make(port: int | None = None) -> Service:
        s = Service(tmp_path, port or free_port())
        made.append(s)
        return s

    yield make
    for s in made:
        s.bootout()


def health(port: int, seconds: float = 60) -> httpx.Response | None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            return httpx.get(f"http://127.0.0.1:{port}/v1/health", timeout=2)
        except httpx.TransportError:
            time.sleep(0.3)
    return None


def test_earshotd_serves_the_socket_launchd_bound(service):
    s = service()
    s.render()
    s.bootstrap()
    r = health(s.port)
    assert r is not None, s.log_text()
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert "state = running" in s.print(), s.print()


def test_a_config_listen_that_is_not_launchds_socket_never_answers_and_is_named(service):
    s = service()
    s.render()
    other = free_port()
    s.write_config(other)  # launchd holds s.port; the config now says another
    s.bootstrap()
    log = s.wait_for_log("install.sh")
    assert f"127.0.0.1:{s.port}" in log and f"127.0.0.1:{other}" in log, log
    assert "deploy/macos/install.sh" in log
    with pytest.raises(httpx.HTTPError):
        httpx.get(f"http://127.0.0.1:{s.port}/v1/health", timeout=3)
