"""earshotd.models: the pinned pyannote files are checked and placed whole or not at all."""

import dataclasses
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

os.environ.setdefault("HF_HUB_OFFLINE", "1")

from model_paths import SNAPSHOT
from test_audio import fixture_frames

from earshotd import diarize, models
from earshotd.audio import decode_opus

ROOT = Path(__file__).resolve().parents[2]

# Three small files and their sha256, worked out by hand with `shasum -a 256`, not by the code.
CONTENT = {
    "config.yaml": b"alpha\n",
    "embedding/pytorch_model.bin": b"bravo\n",
    "plda/plda.npz": b"charlie\n",
}
PINS = {
    "config.yaml": "b6a98d9ce9a2d9149288fa3df42d377c3e42737afdcdaf714e33c0a100b51060",
    "embedding/pytorch_model.bin": "5da8f23decf397b13f4f55b6fb8a61936238bfe08ed9d901132974f1beccc45c",
    "plda/plda.npz": "999d1d048ee9123272dd9b718680551c83e867935b47c2650e6906dc22674e47",
}


@pytest.fixture
def small_pins(monkeypatch):
    """DIARIZATION's file pins replaced by the three above."""
    monkeypatch.setattr(
        models, "DIARIZATION", dataclasses.replace(models.DIARIZATION, files=PINS)
    )


def write_copy(directory: Path, content: dict[str, bytes] = CONTENT) -> Path:
    for rel, data in content.items():
        path = directory / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return directory


def snapshot_like_hf_cache(base: Path, content: dict[str, bytes] = CONTENT) -> Path:
    """A snapshot directory whose pinned files are symlinks into a sibling blobs/, plus extras."""
    blobs = base / "blobs"
    blobs.mkdir(parents=True)
    snap = base / "snapshots" / "rev"
    for i, (rel, data) in enumerate(content.items()):
        (blobs / f"blob{i}").write_bytes(data)
        link = snap / rel
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(os.path.relpath(blobs / f"blob{i}", link.parent))
    (snap / "README.md").write_text("model card\n")
    (snap / ".cache" / "huggingface").mkdir(parents=True)
    (snap / ".cache" / "huggingface" / "x.metadata").write_text("m\n")
    return snap


def tree(directory: Path) -> dict[str, bytes | None]:
    """Every path below directory (relative) -> its bytes, None for a directory."""
    return {
        str(p.relative_to(directory)): None if p.is_dir() else p.read_bytes()
        for p in sorted(directory.rglob("*"))
    }


def only_names(problems: list[str], bad: str) -> bool:
    return (
        len(problems) == 1
        and bad in problems[0]
        and all(other not in problems[0] for other in PINS if other != bad)
    )


def test_the_real_pins_name_the_five_files_at_the_pinned_revision():
    pins = models.DIARIZATION
    assert pins.revision == "3533c8cf8e369892e6b79ff1bf80f7b0286a54ee"
    assert pins.repo == "pyannote/speaker-diarization-community-1"
    assert set(pins.files) == {
        "config.yaml",
        "embedding/pytorch_model.bin",
        "plda/plda.npz",
        "plda/xvec_transform.npz",
        "segmentation/pytorch_model.bin",
    }
    assert all(len(h) == 64 and h == h.lower() for h in pins.files.values())


def test_check_names_a_file_that_differs_by_one_byte_and_only_it(tmp_path, small_pins):
    copy = write_copy(tmp_path / "copy")
    (copy / "plda/plda.npz").write_bytes(b"charlie\x00")
    problems = models.check(copy)
    assert only_names(problems, "plda/plda.npz"), problems
    (copy / "plda/plda.npz").write_bytes(b"charlie\n")
    (copy / "config.yaml").write_bytes(b"alphb\n")
    assert only_names(models.check(copy), "config.yaml")


def test_check_names_a_pinned_file_that_is_missing(tmp_path, small_pins):
    copy = write_copy(tmp_path / "copy")
    (copy / "embedding/pytorch_model.bin").unlink()
    problems = models.check(copy)
    assert only_names(problems, "embedding/pytorch_model.bin"), problems


def test_check_refuses_a_symlink_though_its_target_has_the_right_content(
    tmp_path, small_pins
):
    copy = write_copy(tmp_path / "copy")
    target = tmp_path / "elsewhere"
    target.write_bytes(CONTENT["config.yaml"])
    (copy / "config.yaml").unlink()
    (copy / "config.yaml").symlink_to(target)
    problems = models.check(copy)
    assert only_names(problems, "config.yaml"), problems


def test_check_passes_all_pinned_files_with_an_extra_file_beside_them(
    tmp_path, small_pins
):
    copy = write_copy(tmp_path / "copy")
    (copy / "README.md").write_text("extra\n")
    assert models.check(copy) == []
    assert models.check(tmp_path / "absent") != []


def test_place_installs_exactly_the_pinned_files_as_regular_files(tmp_path, small_pins):
    snap = snapshot_like_hf_cache(tmp_path / "hf")
    models_dir = tmp_path / "models"
    installed = write_copy(models_dir / models.DIARIZATION.name, {"old.txt": b"old"})
    models.place(snap, models_dir)
    assert installed == models_dir / models.DIARIZATION.name
    assert tree(installed) == {
        "config.yaml": b"alpha\n",
        "embedding": None,
        "embedding/pytorch_model.bin": b"bravo\n",
        "plda": None,
        "plda/plda.npz": b"charlie\n",
    }
    assert not any(p.is_symlink() for p in installed.rglob("*"))
    assert models.check(installed) == []
    assert [p.name for p in models_dir.iterdir()] == [models.DIARIZATION.name]


def test_place_with_one_differing_file_changes_nothing_and_names_it(
    tmp_path, small_pins
):
    snap = snapshot_like_hf_cache(
        tmp_path / "hf", {**CONTENT, "plda/plda.npz": b"charlie!\n"}
    )
    models_dir = tmp_path / "models"
    installed = write_copy(models_dir / models.DIARIZATION.name)
    before = tree(models_dir)
    with pytest.raises(models.ModelError) as e:
        models.place(snap, models_dir)
    assert "plda/plda.npz" in str(e.value)
    assert "config.yaml" not in str(e.value)
    assert tree(models_dir) == before
    assert models.check(installed) == []


def test_place_with_a_file_missing_from_src_names_it_and_changes_nothing(
    tmp_path, small_pins
):
    snap = snapshot_like_hf_cache(tmp_path / "hf")
    (snap / "config.yaml").unlink()
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    with pytest.raises(models.ModelError) as e:
        models.place(snap, models_dir)
    assert "config.yaml" in str(e.value)
    assert list(models_dir.iterdir()) == []


def test_each_revision_is_written_only_in_models_py():
    # The tests hold the expected values as literals, so the search skips earshotd/tests.
    skip = {".git", ".claude", "docs", ".venv", "node_modules", "tests"}
    skip |= {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
    found: dict[str, set[str]] = {
        "3533c8cf8e369892e6b79ff1bf80f7b0286a54ee": set(),
        "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf": set(),
    }
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for name in filenames:
            path = Path(dirpath) / name
            try:
                text = path.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            for rev, where in found.items():
                if rev in text:
                    where.add(str(path.relative_to(ROOT)))
    models_py = "earshotd/src/earshotd/models.py"
    assert found == {rev: {models_py} for rev in found}


def test_model_paths_follow_the_pin_and_the_environment(monkeypatch):
    import importlib

    import model_paths

    revision = "3533c8cf8e369892e6b79ff1bf80f7b0286a54ee"
    try:
        monkeypatch.delenv("EARSHOT_DIARIZATION_MODEL", raising=False)
        default = importlib.reload(model_paths).SNAPSHOT
        assert default == (
            Path.home()
            / ".cache/huggingface/hub/models--pyannote--speaker-diarization-community-1"
            / "snapshots"
            / revision
        )
        monkeypatch.setenv("EARSHOT_DIARIZATION_MODEL", "/somewhere/else")
        assert importlib.reload(model_paths).SNAPSHOT == Path("/somewhere/else")
    finally:
        monkeypatch.undo()
        importlib.reload(model_paths)


@pytest.mark.model("diarization")
def test_the_real_snapshot_passes_check_and_a_placed_copy_diarizes_the_fixture(
    tmp_path,
):
    assert SNAPSHOT.is_dir(), f"snapshot missing at {SNAPSHOT}"
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    placed = models.place(SNAPSHOT, models_dir)
    assert models.check(placed) == []
    audio = decode_opus(fixture_frames()).samples
    result = diarize.diarize(diarize.load_diarizer(placed), audio)
    assert result.turns
    assert all(0 <= t.start_ms < t.end_ms <= 48_100 for t in result.turns)


# The model marker: tests of conftest.py's one setup hook, each in a pytest of its own run under a
# constructed HOME, because the verdicts are taken once when a session starts.

HERE = Path(__file__).resolve().parent
PYANNOTE_DIR = "models--pyannote--speaker-diarization-community-1"
WHISPER_DIR = "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo"
DIARIZATION_TEST = (
    "import pytest\n\n@pytest.mark.model('diarization')\ndef test_marked():\n    pass\n"
)
WHISPER_TEST = DIARIZATION_TEST.replace("diarization", "whisper")


def run_marked_suite(
    tmp_path: Path, source: str, setup=None, override: str | None = None
) -> tuple[subprocess.CompletedProcess, Path]:
    """pytest -rs over `source` beside this directory's conftest, under a fresh empty HOME."""
    suite, home = tmp_path / "suite", tmp_path / "home"
    suite.mkdir(parents=True)
    home.mkdir()
    for name in ("conftest.py", "model_paths.py", "fake_limitless.py"):
        shutil.copy(HERE / name, suite / name)
    (suite / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    model(*names): needs a model\n"
    )
    (suite / "test_case.py").write_text(source)
    if setup is not None:
        setup(home)
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("HF_")
        and k not in ("EARSHOT_DIARIZATION_MODEL", "XDG_CACHE_HOME")
    }
    env["HOME"] = str(home)
    if override is not None:
        env["EARSHOT_DIARIZATION_MODEL"] = override
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-rs", "-q", "."],
        cwd=suite,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return done, home


def test_a_diarization_test_is_skipped_with_the_reason_when_no_model_is_anywhere(
    tmp_path,
):
    done, home = run_marked_suite(tmp_path, DIARIZATION_TEST)
    assert done.returncode == 0, done.stdout
    assert "1 skipped" in done.stdout
    assert str(home / ".cache/huggingface/hub" / PYANNOTE_DIR) in done.stdout
    assert "gated" in done.stdout
    assert "EARSHOT_DIARIZATION_MODEL" in done.stdout
    assert models.DIARIZATION.revision in done.stdout


def test_a_missing_override_fails_the_test_at_setup_naming_the_path(tmp_path):
    missing = tmp_path / "nowhere" / "snapshot"
    done, _ = run_marked_suite(tmp_path, DIARIZATION_TEST, override=str(missing))
    assert done.returncode != 0
    assert "skipped" not in done.stdout
    assert "ERROR at setup" in done.stdout
    assert f"EARSHOT_DIARIZATION_MODEL pointed at {missing}" in done.stdout


def test_a_cache_without_the_pinned_snapshot_fails_naming_the_snapshot_path(tmp_path):
    def other_revision(home: Path) -> None:
        repo = home / ".cache/huggingface/hub" / PYANNOTE_DIR
        (repo / "snapshots" / ("f" * 40)).mkdir(parents=True)

    done, home = run_marked_suite(tmp_path, DIARIZATION_TEST, setup=other_revision)
    assert done.returncode != 0
    assert "skipped" not in done.stdout
    snapshot = (
        home
        / ".cache/huggingface/hub"
        / PYANNOTE_DIR
        / "snapshots"
        / models.DIARIZATION.revision
    )
    assert str(snapshot) in done.stdout


def test_a_symlinked_hub_snapshot_counts_as_present(tmp_path):
    def standard_download(home: Path) -> None:
        repo = home / ".cache/huggingface/hub" / PYANNOTE_DIR
        snap = repo / "snapshots" / models.DIARIZATION.revision
        (repo / "blobs").mkdir(parents=True)
        for i, rel in enumerate(models.DIARIZATION.files):
            (repo / "blobs" / f"blob{i}").write_bytes(b"x")
            link = snap / rel
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(os.path.relpath(repo / "blobs" / f"blob{i}", link.parent))

    source = DIARIZATION_TEST.replace("pass", "open('ran', 'w').close()")
    done, _ = run_marked_suite(tmp_path, source, setup=standard_download)
    assert done.returncode == 0, done.stdout
    assert "1 passed" in done.stdout and "skipped" not in done.stdout
    assert (tmp_path / "suite" / "ran").exists()


def test_a_whisper_test_is_skipped_naming_the_cache_directory_when_absent(tmp_path):
    done, home = run_marked_suite(tmp_path, WHISPER_TEST)
    assert done.returncode == 0, done.stdout
    assert "1 skipped" in done.stdout
    assert str(home / ".cache/huggingface/hub" / WHISPER_DIR) in done.stdout


def test_an_unresolvable_whisper_cache_fails_naming_that_download_model_could_not(
    tmp_path,
):
    def empty_repo(home: Path) -> None:
        (home / ".cache/huggingface/hub" / WHISPER_DIR).mkdir(parents=True)

    done, _ = run_marked_suite(tmp_path, WHISPER_TEST, setup=empty_repo)
    assert done.returncode != 0
    assert "skipped" not in done.stdout
    assert "download_model could not resolve" in done.stdout


def test_a_whisper_snapshot_of_another_revision_fails_naming_both_revisions(tmp_path):
    other = "e" * 40

    def other_revision(home: Path) -> None:
        repo = home / ".cache/huggingface/hub" / WHISPER_DIR
        (repo / "snapshots" / other).mkdir(parents=True)
        (repo / "refs").mkdir()
        (repo / "refs" / "main").write_text(other)

    done, _ = run_marked_suite(tmp_path, WHISPER_TEST, setup=other_revision)
    assert done.returncode != 0
    assert "skipped" not in done.stdout
    assert other in done.stdout and models.WHISPER.revision in done.stdout


def test_both_verdicts_are_taken_before_any_test_runs(tmp_path):
    source = (
        "import os\nfrom pathlib import Path\nimport pytest\n\n"
        "def test_a_creates_the_cache_directory():\n"
        "    Path(os.path.expanduser("
        f"'~/.cache/huggingface/hub/{PYANNOTE_DIR}')).mkdir(parents=True)\n\n"
        "@pytest.mark.model('diarization')\ndef test_marked():\n    pass\n"
    )
    done, _ = run_marked_suite(tmp_path, source)
    assert done.returncode == 0, done.stdout
    assert "1 passed, 1 skipped" in done.stdout


FIXTURE_TEST = (
    "import pytest\n\n"
    "@pytest.fixture(scope='module')\ndef recorder():\n"
    "    open('fixture-ran', 'w').close()\n\n"
    "@pytest.mark.model('diarization')\ndef test_marked(recorder):\n    pass\n"
)


def test_the_verdict_comes_before_any_fixture_of_the_test(tmp_path):
    done, _ = run_marked_suite(tmp_path / "absent", FIXTURE_TEST)
    assert "1 skipped" in done.stdout
    assert not (tmp_path / "absent" / "suite" / "fixture-ran").exists()
    done, _ = run_marked_suite(
        tmp_path / "broken", FIXTURE_TEST, override=str(tmp_path / "nowhere")
    )
    assert done.returncode != 0 and "ERROR at setup" in done.stdout
    assert not (tmp_path / "broken" / "suite" / "fixture-ran").exists()


def test_a_broken_model_fails_a_combined_marker_though_the_other_is_absent(tmp_path):
    def other_revision(home: Path) -> None:
        repo = home / ".cache/huggingface/hub" / WHISPER_DIR
        (repo / "snapshots" / ("e" * 40)).mkdir(parents=True)
        (repo / "refs").mkdir()
        (repo / "refs" / "main").write_text("e" * 40)

    source = DIARIZATION_TEST.replace("'diarization'", "'diarization', 'whisper'")
    done, _ = run_marked_suite(tmp_path, source, setup=other_revision)
    assert done.returncode != 0
    assert "skipped" not in done.stdout
    assert models.WHISPER.revision in done.stdout


def test_a_model_marker_naming_no_model_fails_the_test(tmp_path):
    source = (
        "import pytest\n\n@pytest.mark.model('llama')\ndef test_marked():\n    pass\n"
    )
    done, _ = run_marked_suite(tmp_path, source)
    assert done.returncode != 0 and "llama" in done.stdout


@pytest.mark.parametrize("argv", [["fetch"], ["place", "snapshot"]])
def test_a_missing_models_directory_is_refused_before_any_prompt(
    tmp_path, argv, capsys
):
    # A fetch into a directory install never made used to raise from tempfile after the token
    # was already pasted; it must refuse first and read nothing from stdin.
    missing = tmp_path / "no-such-models"
    stdin = tmp_path / "stdin"
    stdin.write_text("hf_must_not_be_read\n")
    with stdin.open() as f, pytest.MonkeyPatch.context() as mp:
        mp.setattr("sys.stdin", f)
        assert models.main([*argv, str(missing)]) == 1
        assert f.tell() == 0
    assert "is not a directory" in capsys.readouterr().err
    assert not missing.exists()
