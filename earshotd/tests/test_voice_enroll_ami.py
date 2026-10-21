"""C9: a voice enrolled from the first window names its speaker in the second window.

Shares the run in test_linking_ami.py: FEE013 is enrolled with `earshot voice add` from a range
inside the first window only, then the second window is processed.
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import sqlite3

import pytest
from pyannote.core import Annotation, Segment
from test_linking_ami import BASE, ami_run

from earshotd.pipeline import SegmentRow

pytestmark = pytest.mark.model("diarization", "whisper")


def reference_speaker(reference: Annotation, seg: SegmentRow) -> str | None:
    """The reference speaker with the most speech inside a transcript segment."""
    window = Segment((seg.start_ms - BASE) / 1000, (seg.end_ms - BASE) / 1000)
    cropped = reference.crop(window)
    if not cropped.labels():
        return None
    return max(cropped.labels(), key=lambda lab: cropped.label_timeline(lab).duration())


def test_the_enrolment_range_lies_inside_the_first_window():
    run = ami_run()
    start, end = run.enrolled
    assert 0 <= start < end <= run.split_s
    conn = sqlite3.connect(run.config.db_path)
    try:
        assert conn.execute("SELECT name FROM voices").fetchall() == [("FEE013",)]
    finally:
        conn.close()


def test_second_window_names_fee013_and_only_fee013():
    run = ami_run()
    by_ref: dict[str, list[SegmentRow]] = {}
    for seg in run.second.segments:
        speaker = reference_speaker(run.reference, seg)
        if speaker:
            by_ref.setdefault(speaker, []).append(seg)
    fee = by_ref["FEE013"]
    named = sum(s.name == "FEE013" for s in fee)
    others = [s for spk, segs in by_ref.items() if spk != "FEE013" for s in segs]
    wrong = sum(s.name == "FEE013" for s in others)
    print(f"FEE013 named {named}/{len(fee)}; others named FEE013 {wrong}/{len(others)}")
    assert len(fee) >= 4 and len(others) >= 10
    assert named >= 0.9 * len(fee)
    assert wrong <= 0.05 * len(others)
    assert {s.name for s in others} <= {None, "FEE013"}
