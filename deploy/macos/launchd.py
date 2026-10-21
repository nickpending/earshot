"""The launchd job for earshotd on a Mac: what deploy/macos/install.sh runs through the service venv.

    $EARSHOT_PY -I deploy/macos/launchd.py paths <config> <root>
    $EARSHOT_PY -I deploy/macos/launchd.py render <config> <root> <home> <log> <label> <plist-out> <profile-out>

`paths` points the data_dir, whisper_model and diarization_model lines of a just-seeded config at
<root>. `render` loads the config the way earshotd does, refuses what the Mac service cannot run
safely, and writes the sandbox profile and the launchd plist. It reads the config itself, so no
token is ever on an argv. A refusal prints one line to stderr, exits 1 and writes nothing.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import sys
import tempfile
import tomllib
from pathlib import Path

from earshotd.app import LAUNCHD_SOCKET
from earshotd.config import Config, ConfigError, load
from earshotd.models import DIARIZATION, WHISPER

LOOPBACK = {"127.0.0.1": "IPv4", "::1": "IPv6"}
# The only mach services the pipeline looks up (measured on macOS 26 under the profile).
MACH_SERVICES = (
    "com.apple.system.notification_center",
    "com.apple.logd",
    "com.apple.system.opendirectoryd.libinfo",
)
ENTRY = "from earshotd.app import main; main()"


class Refused(Exception):
    pass


def model_dirs(root: str) -> dict[str, str]:
    """The config key and value of each model under <root>/models."""
    return {
        "whisper_model": f"{root}/models/{WHISPER.name}",
        "diarization_model": f"{root}/models/{DIARIZATION.name}",
    }


def rewrite_paths(text: str, root: str) -> str:
    """`text` with the data_dir, whisper_model and diarization_model lines pointed at <root>."""
    wanted = {"data_dir": f"{root}/data", **model_dirs(root)}
    for key, value in wanted.items():
        pattern = re.compile(rf"^{key} = .*$", re.MULTILINE)
        if len(pattern.findall(text)) != 1:
            raise Refused(f"the config needs exactly one {key} = line to point at {root}")
        line = f"{key} = {json.dumps(value)}"
        text = pattern.sub(lambda _: line, text)
    return text


def write_whole(path: str, data: bytes, mode: int) -> None:
    """Write `data` to `path` all at once: a temporary file in the same directory, then a rename."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".launchd-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        os.unlink(temporary)
        raise


def real(path: str | os.PathLike) -> str:
    """The path with symlinks resolved: a sandbox rule matches the real path."""
    return os.path.realpath(path)


def inside(path: str, parent: str) -> bool:
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def check(config: Config, root: str) -> None:
    """Raise Refused, naming the config key and value, for what the Mac service cannot run."""
    host, _ = config.host_port
    if host.strip("[]") not in LOOPBACK:
        raise Refused(
            f'refusing listen = "{config.listen}": the Mac service listens on 127.0.0.1 or ::1 only'
        )
    models = real(f"{root}/models")
    for key in ("whisper_model", "diarization_model"):
        value = getattr(config, key)
        if not os.path.isabs(value) or not inside(real(value), models) or real(value) == models:
            raise Refused(
                f'refusing {key} = "{value}": the model must be a directory under {root}/models'
            )
    data = real(config.data_dir)
    for name in ("app", "python", "models"):
        protected = real(f"{root}/{name}")
        if inside(data, protected) or inside(protected, data):
            raise Refused(
                f'refusing data_dir = "{config.data_dir}": it overlaps {root}/{name},'
                " which the service must not be able to write"
            )


def quoted(path: str) -> str:
    """An SBPL string for `path`, double quote and backslash escaped."""
    if any(ord(c) < 32 or ord(c) == 127 for c in path):
        raise Refused(f"refusing a path with a control character: {path!r}")
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def ancestors(home: str, paths: list[str]) -> list[str]:
    """The directories from <home> down to the parent of each path under it, each once.

    A path outside the home contributes none: nothing outside the home is denied.
    """
    found: dict[str, None] = {}
    for path in paths:
        if path == home or not inside(path, home):
            continue
        parent = os.path.dirname(path)
        while inside(parent, home):
            found[parent] = None
            parent = os.path.dirname(parent)
    return list(found)


def profile(config: Config, root: str, home: str, config_path: str) -> str:
    """The sandbox-exec profile: no network of its own, no other program, no other mach service."""
    _, port = config.host_port
    r, h = real(root), real(home)
    data, tmp = real(config.data_dir), f"{r}/tmp"
    python = real(f"{root}/app/.venv/bin/python")
    reads = [f"{r}/app", f"{r}/python", f"{r}/models", data, tmp]
    listed = ancestors(h, [r, data, real(config_path)])
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny network*)",
        f'(allow network-inbound (local ip "localhost:{port}"))',
        "(deny file-write*)",
        f'(allow file-write* (subpath {quoted(data)}) (subpath {quoted(tmp)}) (literal "/dev/null"))',
        f"(deny file-read* (subpath {quoted(h)}))",
        "(allow file-read* "
        + " ".join(f"(subpath {quoted(p)})" for p in reads)
        + f" (literal {quoted(real(config_path))}))",
        # launchd starts the job with its working directory at the root, and getcwd reads it
        f"(allow file-read* (literal {quoted(r)}))",
        # path resolution and SQLite stat each directory down to the allowed paths, not list them
        *(
            ["(allow file-read-metadata " + " ".join(f"(literal {quoted(a)})" for a in listed) + ")"]
            if listed
            else []
        ),
        "(deny process-exec*)",
        f"(allow process-exec (literal {quoted(python)}))",
        "(deny mach-lookup)",
        "(allow mach-lookup "
        + " ".join(f'(global-name "{name}")' for name in MACH_SERVICES)
        + ")",
        "(deny signal (target others))",
    ]
    return "\n".join(lines) + "\n"


def plist(config: Config, root: str, config_path: str, log: str, label: str, profile_path: str) -> bytes:
    host, port = config.host_port
    node = host.strip("[]")
    return plistlib.dumps(
        {
            "Label": label,
            "ProgramArguments": [
                "/usr/bin/sandbox-exec",
                "-f",
                profile_path,
                f"{root}/app/.venv/bin/python",
                "-I",
                "-c",
                ENTRY,
            ],
            "Sockets": {
                LAUNCHD_SOCKET: {
                    "SockNodeName": node,
                    "SockServiceName": str(port),
                    "SockType": "stream",
                    "SockFamily": LOOPBACK[node],
                }
            },
            "Umask": 0o077,
            "WorkingDirectory": root,
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 5,
            "EnvironmentVariables": {
                "EARSHOTD_CONFIG": config_path,
                "HF_HUB_OFFLINE": "1",
                "TMPDIR": f"{root}/tmp/",
                "MPLCONFIGDIR": f"{root}/tmp/matplotlib",
            },
            "StandardOutPath": log,
            "StandardErrorPath": log,
        }
    )


def do_paths(config_path: str, root: str) -> None:
    with open(config_path, encoding="utf-8", newline="") as f:
        text = f.read()
    write_whole(config_path, rewrite_paths(text, root).encode(), 0o600)


def do_render(
    config_path: str, root: str, home: str, log: str, label: str, plist_out: str, profile_out: str
) -> None:
    try:
        config = load(config_path)
    except (ConfigError, OSError, tomllib.TOMLDecodeError) as e:
        raise Refused(f"refusing {config_path}: {e}") from None
    check(config, root)
    sb = profile(config, root, home, config_path)
    body = plist(config, root, config_path, log, label, profile_out)
    write_whole(profile_out, sb.encode(), 0o600)
    write_whole(plist_out, body, 0o644)


def main(argv: list[str]) -> int:
    commands = {"paths": (do_paths, 2), "render": (do_render, 7)}
    if not argv or argv[0] not in commands or len(argv) - 1 != commands[argv[0]][1]:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        commands[argv[0]][0](*argv[1:])
    except Refused as e:
        print(f"launchd.py: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
