"""deploy/macos/launchd.py render: what it refuses and the plist it writes. Runs on every platform."""

import importlib.util
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAUNCHD = ROOT / "deploy" / "macos" / "launchd.py"
GOOD = "0123456789abcdef" * 4
WHISPER = "faster-whisper-large-v3-turbo"
DIARIZATION = "speaker-diarization-community-1"


def config_text(root: Path, listen: str = "127.0.0.1:18080", **override: str) -> str:
    values = {
        "listen": f'"{listen}"',
        "data_dir": f'"{root}/data"',
        "whisper_model": f'"{root}/models/{WHISPER}"',
        "diarization_model": f'"{root}/models/{DIARIZATION}"',
        **override,
    }
    lines = [f"{k} = {v}" for k, v in values.items()]
    lines += ["[[tokens]]", f'token = "{GOOD}"', 'scope = "read"']
    return "\n".join(lines) + "\n"


def write_config(tmp_path: Path, root: Path, **kw) -> Path:
    cfg = tmp_path / "earshotd.toml"
    cfg.write_text(config_text(root, **kw))
    cfg.chmod(0o600)
    return cfg


def render(tmp_path: Path, cfg: Path, root: Path, home: Path | None = None):
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    plist, profile = out / "job.plist", out / "job.sb"
    done = subprocess.run(
        [
            sys.executable,
            "-I",
            str(LAUNCHD),
            "render",
            str(cfg),
            str(root),
            str(home or tmp_path / "home"),
            str(tmp_path / "log"),
            "test.label",
            str(plist),
            str(profile),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return done, plist, profile


@pytest.fixture
def root(tmp_path) -> Path:
    r = tmp_path / "root"
    r.mkdir()
    return r


# case -> (listen, other config lines as raw TOML with {root}, the text the one stderr line holds)
REFUSED = {
    "wildcard": ("0.0.0.0:8080", {"insecure_plain_http": "true"}, 'listen = "0.0.0.0:8080"'),
    "localhost": ("localhost:8080", {}, 'listen = "localhost:8080"'),
    "other loopback": ("127.0.0.2:8080", {}, 'listen = "127.0.0.2:8080"'),
    "whisper elsewhere": (
        "127.0.0.1:8080",
        {"whisper_model": '"/elsewhere/whisper"'},
        'whisper_model = "/elsewhere/whisper"',
    ),
    "whisper by name": (
        "127.0.0.1:8080",
        {"whisper_model": '"large-v3-turbo"'},
        'whisper_model = "large-v3-turbo"',
    ),
    "diarization elsewhere": (
        "127.0.0.1:8080",
        {"diarization_model": '"/elsewhere/pyannote"'},
        'diarization_model = "/elsewhere/pyannote"',
    ),
    "data in models": (
        "127.0.0.1:8080",
        {"data_dir": '"{root}/models/data"'},
        'data_dir = "{root}/models/data"',
    ),
    "data in app": (
        "127.0.0.1:8080",
        {"data_dir": '"{root}/app/data"'},
        'data_dir = "{root}/app/data"',
    ),
    "data holds the app": (
        "127.0.0.1:8080",
        {"data_dir": '"{root}"'},
        'data_dir = "{root}"',
    ),
}


@pytest.mark.parametrize("case", REFUSED)
def test_a_config_the_mac_cannot_run_safely_is_refused_and_nothing_is_written(
    tmp_path, root, case
):
    listen, extra, named = REFUSED[case]
    extra = {k: v.replace("{root}", str(root)) for k, v in extra.items()}
    cfg = tmp_path / "earshotd.toml"
    cfg.write_text(config_text(root, listen, **extra))
    cfg.chmod(0o600)
    out = tmp_path / "out"
    out.mkdir()
    (out / "job.plist").write_bytes(b"old plist")
    (out / "job.sb").write_bytes(b"old profile")
    done, plist, profile = render(tmp_path, cfg, root)
    assert done.returncode == 1, done.stderr
    assert len(done.stderr.splitlines()) == 1, done.stderr
    assert named.replace("{root}", str(root)) in done.stderr
    assert plist.read_bytes() == b"old plist"
    assert profile.read_bytes() == b"old profile"


def test_a_refusal_with_no_files_there_writes_none(tmp_path, root):
    cfg = write_config(tmp_path, root, listen="127.0.0.2:8080")
    done, plist, profile = render(tmp_path, cfg, root)
    assert done.returncode == 1
    assert not plist.exists() and not profile.exists()


def expected_plist(root: Path, profile: Path, cfg: Path, node: str, port: str, family: str):
    return {
        "Label": "test.label",
        "ProgramArguments": [
            "/usr/bin/sandbox-exec",
            "-f",
            str(profile),
            f"{root}/app/.venv/bin/python",
            "-I",
            "-c",
            "from earshotd.app import main; main()",
        ],
        "Sockets": {
            "Listeners": {
                "SockNodeName": node,
                "SockServiceName": port,
                "SockType": "stream",
                "SockFamily": family,
            }
        },
        "Umask": 63,
        "WorkingDirectory": str(root),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 5,
        "EnvironmentVariables": {
            "EARSHOTD_CONFIG": str(cfg),
            "HF_HUB_OFFLINE": "1",
            "TMPDIR": f"{root}/tmp/",
            "MPLCONFIGDIR": f"{root}/tmp/matplotlib",
        },
        "StandardOutPath": str(cfg.parent / "log"),
        "StandardErrorPath": str(cfg.parent / "log"),
    }


@pytest.mark.parametrize(
    "listen, node, port, family",
    [
        ("127.0.0.1:18080", "127.0.0.1", "18080", "IPv4"),
        ("[::1]:18081", "::1", "18081", "IPv6"),
    ],
)
def test_the_plist_holds_exactly_the_derived_values(
    tmp_path, root, listen, node, port, family
):
    cfg = write_config(tmp_path, root, listen=listen)
    done, plist, profile = render(tmp_path, cfg, root)
    assert done.returncode == 0, done.stderr
    assert plistlib.loads(plist.read_bytes()) == expected_plist(
        root, profile, cfg, node, port, family
    )
    assert profile.stat().st_mode & 0o777 == 0o600


def load_launchd():
    spec = importlib.util.spec_from_file_location("launchd_under_test", LAUNCHD)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_metadata_literals_are_every_directory_from_the_home_down_to_each_allowed_path():
    ancestors = load_launchd().ancestors
    root = "/h/Library/Application Support/earshotd"
    config = "/h/.config/earshot/earshotd.toml"
    inside = ancestors("/h", [root, "/h/elsewhere/data", config])
    expected = [
        "/h",
        "/h/Library",
        "/h/Library/Application Support",
        "/h/elsewhere",
        "/h/.config",
        "/h/.config/earshot",
    ]
    assert sorted(inside) == sorted(expected)
    outside = ancestors("/h", [root, "/srv/data", config])
    assert sorted(outside) == sorted(expected[:3] + expected[4:])
