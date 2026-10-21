"""Where the gated-model tests find the pinned models, and whether they are there.

`diarization_state()` and `whisper_state()` judge presence only, never content: a standard
Hugging Face download keeps snapshot files as symlinks that models.check refuses, and the
content pin is test_models.py's job through models.place.
"""

import os
from pathlib import Path
from typing import Literal, NamedTuple

from huggingface_hub import constants

from earshotd import models

OVERRIDE = os.environ.get("EARSHOT_DIARIZATION_MODEL")
DIARIZATION_REPO = Path(
    f"~/.cache/huggingface/hub/models--{models.DIARIZATION.repo.replace('/', '--')}"
).expanduser()
SNAPSHOT = (
    Path(OVERRIDE).expanduser()
    if OVERRIDE is not None
    else DIARIZATION_REPO / "snapshots" / models.DIARIZATION.revision
)


class State(NamedTuple):
    status: Literal["present", "absent", "broken"]
    reason: str = ""


def diarization_state() -> State:
    """absent: no pointer to the model anywhere; broken: a pointer to nothing; else present."""
    if OVERRIDE is None and not DIARIZATION_REPO.is_dir():
        return State(
            "absent",
            f"the pyannote model is gated and is not on this machine: {DIARIZATION_REPO} does"
            f" not exist. Accept the terms at https://huggingface.co/{models.DIARIZATION.repo},"
            f" fetch revision {models.DIARIZATION.revision} (python -m earshotd.models fetch"
            " <dir>), and set EARSHOT_DIARIZATION_MODEL to <dir>/"
            f"{models.DIARIZATION.name}, or place it in the hub cache",
        )
    if not SNAPSHOT.is_dir():
        if OVERRIDE is not None:
            where = f"EARSHOT_DIARIZATION_MODEL pointed at {SNAPSHOT}, which is not a directory"
        else:
            where = (
                f"the hub cache holds {DIARIZATION_REPO} but not the snapshot for revision"
                f" {models.DIARIZATION.revision}: {SNAPSHOT} is not a directory"
            )
        return State("broken", where)
    return State("present")


def whisper_state() -> State:
    """absent: no whisper in the hub cache download_model reads; broken: not the pinned one."""
    repo_dir = (
        Path(constants.HF_HUB_CACHE)
        / f"models--{models.WHISPER.repo.replace('/', '--')}"
    )
    if not repo_dir.is_dir():
        return State(
            "absent",
            f"whisper large-v3-turbo is not on this machine: {repo_dir} does not exist. Run"
            " python -c 'from faster_whisper.utils import download_model;"
            ' download_model("large-v3-turbo")\' to fetch it',
        )
    from faster_whisper.utils import download_model

    try:
        snapshot = download_model("large-v3-turbo", local_files_only=True)
    except OSError as e:
        return State(
            "broken",
            f"download_model could not resolve a snapshot in {repo_dir}:"
            f" {type(e).__name__}: {e}",
        )
    if Path(snapshot).name != models.WHISPER.revision:
        return State(
            "broken",
            f"download_model resolved {snapshot}, revision {Path(snapshot).name}, but the pin"
            f" is {models.WHISPER.revision}",
        )
    return State("present")
