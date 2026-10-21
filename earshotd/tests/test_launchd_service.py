"""The sandboxed launchd service end to end (macOS only): the fixture becomes segments, and the
kernel's sandbox log shows nothing denied beyond what the design allows.

The root is laid out as deploy/macos/install.sh lays it out: earshotd's code and a venv synced by
uv into <root>/app with its Python in <root>/python, the two pinned models under <root>/models,
a 0600 config, and the plist and profile deploy/macos/launchd.py renders, run as a throwaway agent.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest
from faster_whisper.utils import download_model
from launchd_layout import build
from model_paths import SNAPSHOT
from test_launchd_render import DIARIZATION, LAUNCHD, WHISPER

from earshotd import models

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures" / "say-test.pages.v1"
CONTENT_TYPE = "application/vnd.earshot.pages.v1"
UPLOAD, READ = "a1" * 32, "b2" * 32
DOMAIN = f"gui/{os.getuid()}"

pytestmark = [
    pytest.mark.skipif(sys.platform != "darwin", reason="launchd and sandbox-exec are macOS"),
    pytest.mark.skipif(not SNAPSHOT.exists(), reason=f"pinned pyannote snapshot absent: {SNAPSHOT}"),
]


def run(args: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=True, **kw)


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def service(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("service")
    home, root, config, log = build(tmp)
    label = f"test.earshot.{uuid.uuid4().hex[:12]}"
    port = free_port()
    plist, profile = tmp / f"{label}.plist", root / "earshotd.sb"
    shutil.copytree(
        ROOT / "earshotd" / "src", root / "app" / "src", ignore=shutil.ignore_patterns("__pycache__")
    )
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy(ROOT / "earshotd" / name, root / "app" / name)
    env = {
        **os.environ,
        "UV_PYTHON_INSTALL_DIR": str(root / "python"),
        "UV_PYTHON_PREFERENCE": "only-managed",
        "UV_PROJECT_ENVIRONMENT": str(root / "app" / ".venv"),
    }
    run(
        ["uv", "sync", "--project", str(root / "app"), "--frozen", "--no-dev", "--compile-bytecode"],
        env=env,
    )
    python = str(root / "app" / ".venv" / "bin" / "python")
    stdlib = run([python, "-I", "-c", "import sysconfig; print(sysconfig.get_path('stdlib'))"]).stdout.strip()
    # as install.sh does: -I makes the job ignore PYTHON* variables, and it cannot write
    # __pycache__, so the interpreter's stdlib and earshotd's own code are compiled up front
    run([python, "-I", "-m", "compileall", "-q", stdlib, str(root / "app" / "src")])
    models.place(SNAPSHOT, root / "models")
    shutil.copytree(
        download_model("large-v3-turbo", local_files_only=True), root / "models" / WHISPER
    )
    config.write_text(
        f'listen = "127.0.0.1:{port}"\ntimezone = "America/Los_Angeles"\ndata_dir = {json.dumps(str(root / "data"))}\n'
        f"whisper_model = {json.dumps(str(root / 'models' / WHISPER))}\n"
        f"diarization_model = {json.dumps(str(root / 'models' / DIARIZATION))}\n"
        f'[[tokens]]\ntoken = "{UPLOAD}"\nscope = "upload"\n'
        f'[[tokens]]\ntoken = "{READ}"\nscope = "read"\n'
    )
    config.chmod(0o600)
    run(
        [python, "-I", str(LAUNCHD), "render", str(config), str(root), str(home), str(log),
         label, str(plist), str(profile)]
    )
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    run(["launchctl", "bootstrap", DOMAIN, str(plist)])
    try:
        yield {
            "root": root,
            "home": home,
            "port": port,
            "label": label,
            "log": log,
            "started": started,
        }
    finally:
        subprocess.run(["launchctl", "bootout", f"{DOMAIN}/{label}"], capture_output=True, check=False)


def job(service) -> dict:
    out = subprocess.run(
        ["launchctl", "print", f"{DOMAIN}/{service['label']}"], capture_output=True, text=True, check=False
    ).stdout
    pid = re.search(r"^\s*pid = (\d+)", out, re.MULTILINE)
    runs = re.search(r"^\s*runs = (\d+)", out, re.MULTILINE)
    return {
        "pid": int(pid.group(1)) if pid else None,
        "runs": int(runs.group(1)) if runs else None,
        "text": out,
    }


def api(service, path: str, token: str | None = None, **kw) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    headers.update(kw.pop("headers", {}))
    return httpx.get(f"http://127.0.0.1:{service['port']}{path}", headers=headers, timeout=10, **kw)


def wait(predicate, seconds: float, what: str):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
        except httpx.TransportError:
            last = None
        if last:
            return last
        time.sleep(1)
    pytest.fail(f"timed out waiting for {what}; last: {last!r}")


def test_the_fixture_becomes_four_segments_under_the_sandbox(service):
    wait(lambda: api(service, "/v1/health").status_code == 200, 120, "health")
    first = job(service)
    posted = httpx.post(
        f"http://127.0.0.1:{service['port']}/v1/pages",
        content=FIXTURE.read_bytes(),
        headers={"Authorization": f"Bearer {UPLOAD}", "Content-Type": CONTENT_TYPE},
        timeout=30,
    )
    assert posted.status_code == 200, posted.text

    def day():
        days = api(service, "/v1/days", READ).json()
        return next((d for d in days if d["date"] == "2026-10-01" and d["segments"] >= 4), None)

    found = wait(day, 900, "2026-10-01 segments")
    assert found["segments"] == 4
    status = api(service, "/v1/status", READ).json()
    assert status["untranscribed_pages"] == 0
    assert status["worker"]["failures"] == 0
    after = job(service)
    assert after["pid"] == first["pid"] and after["runs"] == 1, after["text"]

    log = service["log"]
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    for path in (service["root"] / "data").rglob("*"):
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == (0o700 if path.is_dir() else 0o600), (path, oct(mode))


def sandbox_denials(service, pid: int) -> list[str]:
    predicate = f'eventMessage CONTAINS "Sandbox:" AND eventMessage CONTAINS "({pid}) deny"'
    out = run(
        ["log", "show", "--start", service["started"], "--style", "compact", "--predicate", predicate]
    ).stdout
    return [re.sub(r".*\) deny\(\d+\) ", "", ln) for ln in out.splitlines() if ") deny(" in ln]


def test_the_kernel_log_shows_no_denial_the_design_does_not_allow(service):
    pid = job(service)["pid"]
    assert pid is not None
    denied = sandbox_denials(service, pid)
    assert denied, "the log holds the job's harmless denials, so it is readable"
    assert not [d for d in denied if d.startswith("network-outbound")], denied
    assert not [d for d in denied if d.startswith("mach-lookup")], denied
    assert not [d for d in denied if "__pycache__" in d], denied
    root = service["root"].resolve()
    ancestors = {str(p) for p in (root, *root.parents) if p.is_relative_to(service["home"].resolve())}
    reads = [d for d in denied if d.startswith("file-read")]
    assert not [d for d in reads if d.split(" ", 1)[1] in ancestors], reads
    assert not [d for d in reads if d.split(" ", 1)[1].startswith(f"{root}/")], reads
    assert {d for d in denied if d.startswith("process-exec")} <= {
        "process-exec* /usr/sbin/system_profiler"
    }, denied
    assert {d for d in denied if d.startswith("network")} <= {"network-bind local:*:0"}, denied
