"""The tracked tree names none of the operator's machines, accounts, domain or addresses.

The names themselves never appear in the repository: they live in a private list on the
operator's machine (EARSHOT_PRIVATE_TERMS, default ~/.config/earshot/private-terms), so the
guard runs in the operator's pre-push gate and skips, saying why, everywhere the list is absent
(public CI, a contributor's checkout). Every tracked file is scanned, work records included, so
a new record that names a host blocks the push until it is written generically.

List format, one entry per line, `#` starts a comment:
    word <name>             a whole word, case-insensitive
    text <substring>        a substring, case-insensitive
    allow <path> <line>     an exact stripped line that may stay; an entry matching no line fails
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load_terms(path: Path) -> tuple[re.Pattern[str], set[tuple[str, str]]]:
    words, texts, allowed = [], [], set()
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        kind, _, rest = line.partition(" ")
        if kind == "word":
            words.append(re.escape(rest.strip()))
        elif kind == "text":
            texts.append(re.escape(rest.strip()))
        elif kind == "allow":
            where, _, text = rest.strip().partition(" ")
            allowed.add((where, text.strip()))
        else:
            raise ValueError(f"{path}: unknown entry {line!r}")
    parts = ([r"\b(?:" + "|".join(words) + r")\b"] if words else []) + texts
    if not parts:
        raise ValueError(f"{path} lists no names")
    return re.compile("|".join(parts), re.IGNORECASE), allowed


def scan(
    root: Path, pattern: re.Pattern[str], allowed: set[tuple[str, str]]
) -> list[str]:
    """What is wrong in the tracked files under `root`: one entry per problem, [] when clean."""
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=False,
        capture_output=True,
        text=True,
    )
    if listed.returncode != 0:
        return [f"git could not list the files in {root}: {listed.stderr.strip()}"]
    problems, seen = [], set()
    for path in sorted(p for p in listed.stdout.split("\0") if p):
        try:
            data = (Path(root) / path).read_bytes()
        except OSError:
            continue  # tracked but deleted from the working tree
        if b"\0" in data:
            continue
        for number, line in enumerate(data.decode(errors="replace").splitlines(), 1):
            if not pattern.search(line):
                continue
            if (path, line.strip()) in allowed:
                seen.add((path, line.strip()))
            else:
                problems.append(f"{path}:{number}: {line.strip()}")
    problems += [
        f"allowlist entry matches no line: {entry}" for entry in sorted(allowed - seen)
    ]
    return problems


def tracked_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "scratch"
    root.mkdir()
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    for args in (["init", "-q"], ["add", "-A"]):
        subprocess.run(["git", "-C", str(root), *args], check=True)
    return root


def test_the_guard_names_each_unlisted_hit_and_each_stale_entry(tmp_path):
    terms = tmp_path / "terms"
    terms.write_text(
        "# example list\n"
        "word hostone\n"
        "word hosttwo\n"
        "text 10.9.\n"
        "allow doc.md hosttwo is allowed here\n"
        "allow gone.md hostone was here\n"
    )
    pattern, allowed = load_terms(terms)
    root = tracked_repo(
        tmp_path,
        {
            "doc.md": "deployed to hostone\nHostOne proxies it\nhostones is another word\n"
            "10.9.8.7\nhosttwo is allowed here\n",
            "docs/work/x.md": "hostone\n",
        },
    )
    problems = scan(root, pattern, allowed)
    text = "\n".join(problems)
    assert len(problems) == 5, text
    assert "doc.md:1:" in text and "doc.md:2:" in text and "doc.md:4:" in text
    assert "docs/work/x.md:1:" in text, "work records are scanned too"
    assert "gone.md" in text and "matches no line" in text
    assert "doc.md:3" not in text and "doc.md:5" not in text


def test_a_list_with_an_unknown_entry_or_no_names_is_refused(tmp_path):
    bad = tmp_path / "bad"
    bad.write_text("name hostone\n")
    with pytest.raises(ValueError, match="unknown entry"):
        load_terms(bad)
    empty = tmp_path / "empty"
    empty.write_text("# nothing\nallow a b\n")
    with pytest.raises(ValueError, match="lists no names"):
        load_terms(empty)


def test_a_directory_git_cannot_list_fails_instead_of_skipping(tmp_path):
    pattern = re.compile("hostone")
    problems = scan(tmp_path, pattern, set())
    assert len(problems) == 1
    assert "git could not list the files" in problems[0]


def test_the_tracked_tree_names_none_of_the_operators_machines():
    path = Path(
        os.environ.get("EARSHOT_PRIVATE_TERMS", "~/.config/earshot/private-terms")
    ).expanduser()
    if not path.is_file():
        pytest.skip(
            f"no private term list at {path}; this guard runs on the operator's machine"
        )
    pattern, allowed = load_terms(path)
    assert scan(ROOT, pattern, allowed) == []
