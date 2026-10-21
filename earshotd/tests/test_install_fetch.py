"""install_diarization through real bash: the gated download against a local fake Hugging Face
hub, the token handling, and the reruns that ask for nothing."""

import hashlib
import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from test_models import snapshot_like_hf_cache, tree

from earshotd import models

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "deploy" / "install-lib.sh"
BASH = shutil.which("bash")
NAME = models.DIARIZATION.name

pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash")

PINNED = {
    "config.yaml": b"config: small\n",
    "embedding/pytorch_model.bin": b"embedding weights\n",
    "plda/plda.npz": b"plda\n",
    "plda/xvec_transform.npz": b"xvec\n",
    "segmentation/pytorch_model.bin": b"segmentation weights\n",
}
PINS = {rel: hashlib.sha256(data).hexdigest() for rel, data in PINNED.items()}
OTHERS = {
    ".gitattributes": b"*.bin filter=lfs\n",
    "README.md": b"model card\n",
    "reproducibility.md": b"notes\n",
    "plda/README.md": b"plda card\n",
    "embedding/README.md": b"embedding card\n",
}
ALL = {**PINNED, **OTHERS}
assert len(ALL) == 10


class Hub:
    """A fake Hugging Face hub: one gated repo at the pinned revision, served only to `token`."""

    def __init__(self, token: str):
        hub = self
        self.token = token
        self.requests: list[tuple[str, str]] = []
        repo, rev = models.DIARIZATION.repo, models.DIARIZATION.revision

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _serve(self, send_body: bool):
                path = self.path.split("?")[0]
                hub.requests.append((self.command, path))
                if self.headers.get("Authorization") != f"Bearer {hub.token}":
                    self._send(401, b'{"error": "Invalid credentials"}', send_body)
                    return
                if path == f"/api/models/{repo}/revision/{rev}":
                    info = {
                        "id": repo,
                        "modelId": repo,
                        "sha": rev,
                        "gated": "auto",
                        "private": False,
                        "siblings": [{"rfilename": rel} for rel in ALL],
                    }
                    self._send(200, json.dumps(info).encode(), send_body)
                    return
                if path == f"/api/models/{repo}/tree/{rev}":
                    listing = [
                        {
                            "type": "file",
                            "path": rel,
                            "size": len(data),
                            "oid": hashlib.sha1(data).hexdigest(),
                        }
                        for rel, data in ALL.items()
                    ]
                    self._send(200, json.dumps(listing).encode(), send_body)
                    return
                prefix = f"/{repo}/resolve/{rev}/"
                if path.startswith(prefix) and path[len(prefix) :] in ALL:
                    data = ALL[path[len(prefix) :]]
                    etag = hashlib.sha1(data).hexdigest()
                    self._send(
                        200,
                        data,
                        send_body,
                        {"ETag": f'"{etag}"', "X-Repo-Commit": rev},
                    )
                    return
                self._send(404, b'{"error": "not found"}', send_body)

            def _send(self, code, body, send_body, headers=None):
                self.send_response(code)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if send_body:
                    self.wfile.write(body)

            def do_GET(self):
                self._serve(True)

            def do_HEAD(self):
                self._serve(False)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.endpoint = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def downloaded(self) -> set[str]:
        prefix = f"/{models.DIARIZATION.repo}/resolve/{models.DIARIZATION.revision}/"
        return {p[len(prefix) :] for _, p in self.requests if p.startswith(prefix)}


# Records its own argv and environment, then runs the real python. The models entry runs with the
# pins swapped for the test's, in the same process that does the work.
WRAPPER = """#!{python}
import json, os, sys
with open({log!r}, "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "env": dict(os.environ)}}) + "\\n")
args = sys.argv[1:]
if args[:3] == ["-I", "-m", "earshotd.models"]:
    code = (
        "import dataclasses, json, os, sys\\n"
        "from earshotd import models\\n"
        "pins = json.loads(os.environ['EARSHOT_TEST_PINS'])\\n"
        "models.DIARIZATION = dataclasses.replace(models.DIARIZATION, files=pins)\\n"
        "sys.exit(models.main(" + repr(args[3:]) + "))\\n"
    )
    args = ["-I", "-c", code]
os.execv({python!r}, [{python!r}, *args])
"""


class Run:
    def __init__(self, tmp_path: Path):
        self.root = tmp_path / "scratch"
        self.models = self.root / "models"
        self.tmp = self.root / "tmp"
        self.log = self.root / "calls.jsonl"
        for d in (self.models, self.tmp):
            d.mkdir(parents=True)
        self.wrapper = self.root / "py"
        self.wrapper.write_text(
            WRAPPER.format(python=sys.executable, log=str(self.log))
        )
        self.wrapper.chmod(0o755)

    def install_diarization(
        self, endpoint: str, stdin: str | None, snapshot: Path | None = None
    ):
        env = {
            k: v for k, v in os.environ.items() if not k.startswith(("HF_", "HUGGING"))
        }
        env.update(
            EARSHOT_PY=str(self.wrapper),
            EARSHOT_TEST_PINS=json.dumps(PINS),
            HF_ENDPOINT=endpoint,
            TMPDIR=str(self.tmp),
        )
        args = f" {shlex.quote(str(snapshot))}" if snapshot else ""
        assert BASH
        return subprocess.run(
            [
                BASH,
                "-c",
                (
                    f"set -euo pipefail; source {shlex.quote(str(LIB))};"
                    f" install_diarization {shlex.quote(str(self.models))}{args}"
                ),
            ],
            env=env,
            **(
                {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
            ),
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def installed(self) -> Path:
        return self.models / NAME


def dead_endpoint() -> str:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{s.getsockname()[1]}"


def secret_files(root: Path, token: str) -> list[Path]:
    return [
        p for p in root.rglob("*") if p.is_file() and token.encode() in p.read_bytes()
    ]


def test_the_right_token_installs_only_the_five_pinned_files_and_leaves_no_trace(
    tmp_path,
):
    token = "hf_" + secrets.token_hex(24)
    run = Run(tmp_path)
    with Hub(token) as hub:
        done = run.install_diarization(hub.endpoint, token + "\n")
    assert done.returncode == 0, done.stderr + done.stdout
    assert tree(run.installed()) == {
        "config.yaml": PINNED["config.yaml"],
        "embedding": None,
        "embedding/pytorch_model.bin": PINNED["embedding/pytorch_model.bin"],
        "plda": None,
        "plda/plda.npz": PINNED["plda/plda.npz"],
        "plda/xvec_transform.npz": PINNED["plda/xvec_transform.npz"],
        "segmentation": None,
        "segmentation/pytorch_model.bin": PINNED["segmentation/pytorch_model.bin"],
    }
    assert hub.downloaded() == set(PINNED)
    assert [p.name for p in run.models.iterdir()] == [NAME]
    calls = run.calls()
    assert calls, "the wrapper was never used"
    for call in calls:
        assert token not in json.dumps(call)
    hf_homes = {c["env"]["HF_HOME"] for c in calls}
    assert len(hf_homes) == 1 and not Path(hf_homes.pop()).exists()
    assert secret_files(run.root, token) == []
    assert token not in done.stdout + done.stderr


def test_a_token_the_hub_refuses_installs_nothing_and_says_how_to_get_one(tmp_path):
    run = Run(tmp_path)
    with Hub("hf_" + secrets.token_hex(24)) as hub:
        wrong = "hf_" + secrets.token_hex(24)
        done = run.install_diarization(hub.endpoint, wrong + "\n")
    assert done.returncode != 0
    assert "accept the model's terms" in done.stderr and "read token" in done.stderr
    assert wrong not in done.stdout + done.stderr
    assert list(run.models.iterdir()) == []
    assert hub.downloaded() == set()


def test_a_matching_installed_copy_is_kept_with_no_prompt_and_no_network(tmp_path):
    run = Run(tmp_path)
    for rel, data in PINNED.items():
        path = run.installed() / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    before = {
        p: p.stat().st_mtime_ns for p in run.installed().rglob("*") if p.is_file()
    }
    done = run.install_diarization(dead_endpoint(), None)
    assert done.returncode == 0, done.stderr
    assert "matches the pinned revision" in done.stdout
    assert {
        p: p.stat().st_mtime_ns for p in run.installed().rglob("*") if p.is_file()
    } == before
    assert not any(c["argv"][2:4] == ["earshotd.models", "fetch"] for c in run.calls())


def test_a_differing_installed_copy_is_named_and_a_rerun_without_a_token_leaves_it_alone(
    tmp_path,
):
    run = Run(tmp_path)
    for rel, data in PINNED.items():
        path = run.installed() / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (run.installed() / "plda/plda.npz").write_bytes(b"tampered\n")
    before = tree(run.models)
    done = run.install_diarization(dead_endpoint(), None)
    assert done.returncode != 0
    assert "plda/plda.npz" in done.stdout
    assert "no Hugging Face token" in done.stderr
    assert tree(run.models) == before


def test_a_matching_snapshot_installs_with_no_prompt_and_no_network(tmp_path):
    run = Run(tmp_path)
    snap = snapshot_like_hf_cache(tmp_path / "hf", PINNED)
    done = run.install_diarization(dead_endpoint(), None, snapshot=snap)
    assert done.returncode == 0, done.stderr
    assert tree(run.installed())["plda/plda.npz"] == PINNED["plda/plda.npz"]
    assert models_check_ok(run.installed())


def models_check_ok(directory: Path) -> bool:
    return all(
        hashlib.sha256((directory / rel).read_bytes()).hexdigest() == want
        for rel, want in PINS.items()
    )
