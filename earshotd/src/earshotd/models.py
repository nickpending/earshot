"""The two pinned models, and how install gets the gated one in place.

This file is the only home of both revisions. `python -I -m earshotd.models` is the entry
deploy/install-lib.sh calls through root's EARSHOT_PY:

    check <dir>            list the problems of an installed pyannote copy; exit 1 when any
    place <src> <models>   verify <src> and install it as <models>/<pyannote name>
    fetch <models>         read a Hugging Face token, download the pinned files, place them
"""

from __future__ import annotations

import getpass
import hashlib
import shutil
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


class ModelError(Exception):
    pass


@dataclass(frozen=True)
class Model:
    repo: str
    revision: str
    name: str  # the directory under the models directory
    files: Mapping[str, str] = field(default_factory=dict)  # relative path -> sha256


WHISPER = Model(
    repo="mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    revision="0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf",
    name="faster-whisper-large-v3-turbo",
)

DIARIZATION = Model(
    repo="pyannote/speaker-diarization-community-1",
    revision="3533c8cf8e369892e6b79ff1bf80f7b0286a54ee",
    name="speaker-diarization-community-1",
    files={
        "config.yaml": "5ce2bfa9a938dc132cec1172592d65173cbb8f444ea1e4133f10f9391de155be",
        "embedding/pytorch_model.bin": "6f10ff60898a1d185fa22e1d11e0bfa8a92efec811f11bca48cb8cafebefd929",
        "plda/plda.npz": "9b77bcd840692710dd3496f62ecfeed8d8e5f002fd991b785079b244eab7d255",
        "plda/xvec_transform.npz": "325f1ce8e48f7e55e9c8aa47e05d2766b7c48c4b25b8de8dd751e7a4cc5fbe8f",
        "segmentation/pytorch_model.bin": "7ad24338d844fb95985486eb1a464e32d229f6d7a03c9abe60f978bacf3f816e",
    },
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _bad_files(directory: Path) -> dict[str, str]:
    """Pinned relative path -> what is wrong with it, for each pinned file that is not right."""
    bad: dict[str, str] = {}
    for rel, want in DIARIZATION.files.items():
        path = directory / rel
        if path.is_symlink():
            bad[rel] = "is a symlink, not a regular file"
        elif not path.is_file():
            bad[rel] = "is missing"
        elif _sha256(path) != want:
            bad[rel] = "differs from the pinned sha256"
    return bad


def check(directory: str | Path) -> list[str]:
    """Problems with a pyannote copy: empty when each pinned file is a regular file with its sha256."""
    directory = Path(directory)
    return [f"{directory / rel} {why}" for rel, why in _bad_files(directory).items()]


def _existing_dir(models_dir: str | Path) -> Path:
    """The models directory install made with its own owner and mode; never created here."""
    models_dir = Path(models_dir)
    if not models_dir.is_dir():
        raise ModelError(
            f"{models_dir} is not a directory: create it, as install does, first"
        )
    return models_dir


def place(src: str | Path, models_dir: str | Path) -> Path:
    """Install the pinned files of `src` as models_dir/<name>, or change nothing and raise.

    Symlinks in `src` (a Hugging Face cache snapshot holds them) are copied as the files they
    point at. Only the pinned files are copied. They are checked in a staging directory, and the
    installed copy is replaced only when every one matches.
    """
    src, models_dir = Path(src), _existing_dir(models_dir)
    final = models_dir / DIARIZATION.name
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=models_dir))
    try:
        missing = {}
        for rel in DIARIZATION.files:
            source = src / rel
            if not source.is_file():
                missing[rel] = "is missing"
                continue
            target = staging / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        bad = missing | _bad_files(staging)
        if bad:
            raise ModelError(
                f"refusing {src}, nothing installed: "
                + "; ".join(f"{src / rel} {why}" for rel, why in bad.items())
            )
        for path in [staging, *staging.rglob("*")]:
            path.chmod(0o755 if path.is_dir() else 0o644)
        old = None
        if final.exists():
            old = Path(tempfile.mkdtemp(prefix=".old-", dir=models_dir))
            final.rename(old / DIARIZATION.name)
        try:
            staging.rename(final)
        except OSError:
            if old is not None:
                (old / DIARIZATION.name).rename(final)
            raise
        if old is not None:
            shutil.rmtree(old)
        return final
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _read_token() -> str:
    """The Hugging Face token: a hidden prompt on a terminal, else the first line of stdin."""
    if sys.stdin is not None and sys.stdin.isatty():
        return getpass.getpass("Hugging Face read token: ").strip()
    if sys.stdin is None:
        return ""
    return sys.stdin.readline().strip()


def fetch(models_dir: str | Path) -> Path:
    """Download the pinned pyannote files with a token read here, then place them."""
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import HfHubHTTPError, RepositoryNotFoundError

    # Before the prompt: a missing directory must not cost the user their token paste.
    models_dir = _existing_dir(models_dir)
    token = _read_token()
    if not token:
        raise ModelError("no Hugging Face token was given")
    download = Path(tempfile.mkdtemp(prefix=".download-", dir=models_dir))
    try:
        try:
            snapshot_download(
                DIARIZATION.repo,
                revision=DIARIZATION.revision,
                allow_patterns=list(DIARIZATION.files),
                local_dir=download,
                token=token,
            )
        except (RepositoryNotFoundError, HfHubHTTPError) as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if isinstance(e, RepositoryNotFoundError) or status in (401, 403):
                raise ModelError(
                    f"Hugging Face refused the token for {DIARIZATION.repo}: accept the model's"
                    f" terms at https://huggingface.co/{DIARIZATION.repo} and use a read token"
                ) from None
            raise ModelError(f"download of {DIARIZATION.repo} failed: {e}") from None
        return place(download, models_dir)
    finally:
        shutil.rmtree(download, ignore_errors=True)


def main(argv: list[str]) -> int:
    usage = "usage: models check <dir> | place <src> <models> | fetch <models> | revision diarization|whisper"
    try:
        match argv:
            case ["check", directory]:
                problems = check(directory)
                if problems:
                    print("\n".join(problems))
                return 1 if problems else 0
            case ["place", src, models_dir]:
                print(f"installed {place(src, models_dir)}")
                return 0
            case ["fetch", models_dir]:
                print(f"installed {fetch(models_dir)}")
                return 0
            case ["revision", which] if which in ("diarization", "whisper"):
                print((DIARIZATION if which == "diarization" else WHISPER).revision)
                return 0
    except ModelError as e:
        print(f"earshotd models: {e}", file=sys.stderr)
        return 1
    print(usage, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
