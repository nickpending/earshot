"""The worker lock: one file in data_dir, held exclusively for as long as a worker runs.

`earshot rebuild` takes the same lock without waiting, so a reset can never land under a window
in flight. The kernel drops the lock when the holder exits, however it exits.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import IO

NAME = "worker.lock"


class Held(RuntimeError):
    """Another process (or worker) holds the lock."""


def acquire(data_dir: str | Path) -> IO[bytes]:
    """Take the lock without waiting; raise Held when someone has it. Close the file to release."""
    Path(data_dir).mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(Path(data_dir) / NAME, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    f = os.fdopen(fd, "ab")  # held open for the lock
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise Held(f"{Path(data_dir) / NAME} is held") from None
    return f
