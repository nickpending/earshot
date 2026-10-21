"""The in-process worker: runs the pipeline over untranscribed pages for as long as earshotd runs."""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import NamedTuple

from . import lock
from .config import Config
from .pipeline import LocalModels, Models, Pipeline

log = logging.getLogger("earshotd.worker")

POLL_S = 15.0
MAX_FAILURES = 3  # consecutive failed polls before the worker stops and reports


@dataclass
class Status:
    """What /v1/status reports about the worker, so a stalled worker is told apart from a backlog."""

    state: str = "running"  # running | stalled
    failures: int = 0  # consecutive failed polls
    error: str | None = None  # exception type of the last failure, never its message

    def as_dict(self) -> dict:
        return {"state": self.state, "failures": self.failures, "error": self.error}


class Worker(NamedTuple):
    thread: threading.Thread
    stop: threading.Event
    status: Status


def now_ms() -> int:
    return int(time.time() * 1000)


def drain(
    pipeline: Pipeline,
    clock: Callable[[], int] = now_ms,
    stop: threading.Event | None = None,
) -> int:
    """Process every window that is ready, or until `stop` is set; return how many.

    The archive backlog is hours of windows, so `stop` is checked between windows.

    Each committed window is logged with what closed it. Page times, counts and reasons only:
    never the transcript.
    """
    done = 0
    while not (stop and stop.is_set()):
        now = clock()
        result = pipeline.run_once(now)
        if result is None:
            return done
        done += 1
        log.info(
            "window committed: source=%s pages=%d from=%d to=%d span_s=%.0f closed_by=%s "
            "cut_age_s=%.0f segments=%d speakers=%d day=%s",
            result.source,
            len(result.keys),
            result.first_ms,
            result.last_ms,
            (result.last_ms - result.first_ms) / 1000,
            result.reason,
            (now - result.last_ms) / 1000,
            len(result.segments),
            len({s.speaker for s in result.segments if s.speaker}),
            result.day,
        )
    return done


def run(
    pipeline: Pipeline,
    stop: threading.Event,
    clock: Callable[[], int] = now_ms,
    poll_s: float = POLL_S,
    max_failures: int = MAX_FAILURES,
    status: Status | None = None,
) -> bool:
    """Poll until `stop` is set (True) or `max_failures` polls in a row fail (False).

    A window that keeps failing blocks every later one, so the worker gives up loudly instead of
    retrying forever. `status` goes to "stalled" for /v1/status to report, and the pages stay
    untranscribed until earshotd is restarted.
    """
    status = status or Status()
    failures = 0
    while not stop.is_set():
        try:
            drain(pipeline, clock, stop)
            failures = 0
            status.failures = 0
        except Exception:
            failures += 1
            status.failures, status.error = failures, type(sys.exc_info()[1]).__name__
            log.exception("window failed (%d of %d)", failures, max_failures)
            if failures >= max_failures:
                status.state = "stalled"
                log.critical(
                    "worker stopping: the next window failed %d polls in a row; its pages "
                    "stay untranscribed until earshotd is restarted",
                    failures,
                )
                return False
        stop.wait(poll_s)
    return True


def _run_locked(held, *args) -> None:
    try:
        run(*args)
    finally:
        held.close()  # releases the lock, so `earshot rebuild` can run once the worker is gone


def start(config: Config, models: Models | None = None) -> Worker:
    """Start the worker thread, holding the data_dir lock until it ends.

    Raises lock.Held when another worker already holds it.
    """
    held = lock.acquire(config.data_dir)
    stop = threading.Event()
    status = Status()
    pipeline = Pipeline(config, models or LocalModels(config))
    thread = threading.Thread(
        target=_run_locked,
        args=(held, pipeline, stop, now_ms, POLL_S, MAX_FAILURES, status),
        name="earshotd-worker",
        daemon=True,
    )
    thread.start()
    return Worker(thread, stop, status)
