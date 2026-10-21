"""The sandbox profile deploy/macos/launchd.py renders, probed under the real /usr/bin/sandbox-exec."""

import errno
import json
import os
import socket
import subprocess
import sys

import pytest
from launchd_layout import build
from test_launchd_render import DIARIZATION, GOOD, LAUNCHD, WHISPER

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or not os.path.exists(SANDBOX_EXEC),
    reason="sandbox-exec is macOS",
)

# Runs inside the sandbox as the service's interpreter would; prints one JSON object of outcomes.
PROBE = r"""
import ctypes, json, os, socket, sqlite3, subprocess, sys, zoneinfo

args = json.loads(sys.argv[1])
out = {}

def attempt(name, fn):
    try:
        out[name] = fn()
    except OSError as e:
        out[name] = e.errno
    except Exception as e:
        out[name] = f"{type(e).__name__}: {e}"

def connect(address):
    with socket.socket() as s:
        s.settimeout(3)
        s.connect(address)
    return 0

def bind():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
    return 0

def write(path):
    with open(path, "w") as f:
        f.write("x")
    return 0

def read(path):
    with open(path) as f:
        f.read()
    return 0

def sqlite(path):
    db = sqlite3.connect(path)
    db.execute("create table t (x integer)")
    db.execute("insert into t values (1)")
    db.commit()
    value = db.execute("select x from t").fetchone()[0]
    db.close()
    return value

def lookup(name):
    lib = ctypes.CDLL(None)
    port = ctypes.c_uint()
    lib.bootstrap_look_up.argtypes = [ctypes.c_uint, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint)]
    return lib.bootstrap_look_up(ctypes.c_uint.in_dll(lib, "bootstrap_port").value, name.encode(), ctypes.byref(port))

attempt("connect_listener", lambda: connect(("127.0.0.1", args["port"])))
attempt("connect_internet", lambda: connect(("1.1.1.1", 443)))
attempt("bind", bind)
attempt("getaddrinfo", lambda: socket.getaddrinfo("example.com", 443))
for name, path in args["writes"].items():
    attempt("write_" + name, lambda path=path: write(path))
attempt("getcwd", os.getcwd)
attempt("sqlite", lambda: sqlite(args["database"]))
attempt("localtime_real", lambda: os.path.realpath("/etc/localtime"))
if isinstance(out["localtime_real"], str) and "/zoneinfo/" in out["localtime_real"]:
    out["localtime_key"] = out["localtime_real"].split("/zoneinfo/", 1)[1]
    attempt("localtime_zone", lambda: zoneinfo.ZoneInfo(out["localtime_key"]).key)
attempt("list_home", lambda: os.listdir(args["home"]))
attempt("read_secret", lambda: read(args["secret"]))
attempt("read_config", lambda: read(args["config"]))
attempt("exec", lambda: subprocess.run(["/usr/bin/true"], check=True).returncode)
for name in args["lookups"]:
    out["lookup_" + name] = lookup(name)
attempt("signal_other", lambda: os.kill(args["other_pid"], 0))
print(json.dumps(out))
"""

LOOKUPS = {
    "com.apple.coreservices.launchservicesd": 1100,
    "com.apple.coreservices.appleevents": 1100,
    "com.apple.nsurlsessiond": 1100,
    "com.apple.logd": 0,
}


@pytest.fixture
def sandboxed(tmp_path):
    """Render the profile for the real layout under a home named with a space and a double quote."""
    home, root, cfg, _ = build(tmp_path)
    for sub in ("app/.venv/bin", f"models/{WHISPER}", f"models/{DIARIZATION}"):
        (root / sub).mkdir(parents=True)
    (root / "app/.venv/bin/python").symlink_to(os.path.realpath(sys.executable))
    secret = home / "secret.txt"
    secret.write_text("secret")
    cfg.write_text(
        f'listen = "127.0.0.1:18080"\ndata_dir = {json.dumps(f"{root}/data")}\n'
        f"whisper_model = {json.dumps(f'{root}/models/{WHISPER}')}\n"
        f"diarization_model = {json.dumps(f'{root}/models/{DIARIZATION}')}\n"
        f'[[tokens]]\ntoken = "{GOOD}"\nscope = "read"\n'
    )
    cfg.chmod(0o600)
    plist, profile = tmp_path / "job.plist", tmp_path / "job.sb"
    done = subprocess.run(
        [
            sys.executable, "-I", str(LAUNCHD), "render", str(cfg), str(root), str(home),
            str(tmp_path / "log"), "test.label", str(plist), str(profile),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr

    def probe() -> dict:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        args = {
            "port": listener.getsockname()[1],
            "writes": {
                "models": f"{root}/models/x",
                "app": f"{root}/app/x",
                "data": f"{root}/data/x",
                "tmp": f"{root}/tmp/x",
            },
            "home": str(home),
            "database": f"{root}/data/probe.db",
            "secret": str(secret),
            "config": str(cfg),
            "lookups": list(LOOKUPS),
            "other_pid": os.getpid(),
        }
        try:
            ran = subprocess.run(
                [SANDBOX_EXEC, "-f", str(profile), str(root / "app/.venv/bin/python"),
                 "-I", "-c", PROBE, json.dumps(args)],
                cwd=root,  # as launchd runs it: WorkingDirectory is the root
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                # the profile's "others" are the processes outside the job's own session, as
                # launchd runs it: a new session makes this test process one of them
                start_new_session=True,
            )
        finally:
            listener.close()
        assert ran.returncode == 0, (ran.stdout, ran.stderr)
        return {**json.loads(ran.stdout), "root": os.path.realpath(root)}

    return probe


def test_the_profile_confines_the_interpreter_as_designed(sandboxed):
    got = sandboxed()
    # no network of its own
    assert got["connect_listener"] == errno.EPERM
    assert got["connect_internet"] == errno.EPERM
    assert got["bind"] == errno.EPERM
    assert not isinstance(got["getaddrinfo"], list) and got["getaddrinfo"] != 0
    # writes only to data and tmp
    assert got["write_models"] == errno.EPERM
    assert got["write_app"] == errno.EPERM
    assert got["write_data"] == 0
    assert got["write_tmp"] == 0
    # no home reads beyond its own paths
    assert got["list_home"] == errno.EPERM
    assert got["read_secret"] == errno.EPERM
    assert got["read_config"] == 0
    # no other program
    assert got["exec"] == errno.EPERM
    # no mach services but the allowed
    for name, status in LOOKUPS.items():
        assert got[f"lookup_{name}"] == status, name
    # no signals to others
    assert got["signal_other"] == errno.EPERM


def test_the_service_works_from_a_root_inside_the_home(sandboxed):
    got = sandboxed()
    # launchd starts the job with its working directory at the root
    assert got["getcwd"] == got["root"]
    # the store opens under data_dir
    assert got["sqlite"] == 1
    # the host's zone resolves, outside the home where nothing is denied (#2)
    assert "/var/db/timezone/" in got["localtime_real"], got["localtime_real"]
    assert got["localtime_zone"] == got["localtime_key"], got["localtime_zone"]
