"""deploy/macos/launchd.py paths: points a just-seeded config at the service root, nothing else."""

import re
import shlex
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

from test_install_lib import COMMANDS, EXAMPLE, bash, wrappers

from earshotd.config import load

LAUNCHD = Path(__file__).resolve().parents[2] / "deploy" / "macos" / "launchd.py"


def test_paths_points_the_three_locations_at_the_root_and_changes_nothing_else(tmp_path):
    bindir, log = wrappers(tmp_path, COMMANDS)
    cfg = tmp_path / "earshotd.toml"
    seeded = bash(
        f"seed_config {shlex.quote(str(EXAMPLE))} {shlex.quote(str(cfg))}",
        env={"PATH": str(bindir)},
    )
    assert seeded.returncode == 0, seeded.stderr
    cfg.chmod(0o600)
    before = cfg.read_text()
    tokens = {t["scope"]: t["token"] for t in tomllib.loads(before)["tokens"]}
    root = tmp_path / "Application Support" / "earshotd"
    argv_log = log.read_text()
    done = subprocess.run(
        [sys.executable, "-I", str(LAUNCHD), "paths", str(cfg), str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    after = cfg.read_text()

    # hand-derived from earshotd.models' WHISPER and DIARIZATION names
    wanted = {
        "data_dir": f"{root}/data",
        "whisper_model": f"{root}/models/faster-whisper-large-v3-turbo",
        "diarization_model": f"{root}/models/speaker-diarization-community-1",
    }
    parsed = tomllib.loads(after)
    for key, value in wanted.items():
        assert parsed[key] == value
    assert {t["scope"]: t["token"] for t in parsed["tokens"]} == tokens
    assert all(re.fullmatch(r"[0-9a-f]{64}", t) for t in tokens.values())

    changed = [
        (a, b)
        for a, b in zip(before.splitlines(keepends=True), after.splitlines(keepends=True))
        if a != b
    ]
    assert len(before.splitlines()) == len(after.splitlines())
    assert [a.split(" = ")[0] for a, _ in changed] == list(wanted)

    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    config = load(cfg)
    assert str(config.data_dir) == wanted["data_dir"]
    assert config.whisper_model == wanted["whisper_model"]
    assert config.diarization_model == wanted["diarization_model"]
    # no token reached any argv: the seeding wrappers saw none, and paths took only two paths
    for token in tokens.values():
        assert token not in argv_log and token not in done.stderr
