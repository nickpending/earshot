"""Guard: fixtures/pendant-pages hold no real audio, only the synthetic silent frame."""

import numpy as np
from test_records import FIX, raw
from test_silence_pendant_audio import SCRIPT, load_script

from earshotd import records
from earshotd.audio import decode_opus
from earshotd.windowing import split_frames

SILENT = bytes.fromhex("b8" + "ff" * 40)
FRAME_PAGES = {3936: 1, 3937: 72, 49123: 72, 61433: 66, 66064: 75}


def pages() -> dict[int, records.Decoded]:
    return {
        int(p.name.split(".")[0]): records.decode(raw(int(p.name.split(".")[0])))
        for p in FIX.glob("*.storagebuffer.hex")
    }


def frames_of(d: records.Decoded) -> list[bytes]:
    assert d.audio is not None
    return split_frames(d.audio.frames)


def test_the_frame_carrying_pages_are_the_known_five_with_their_counts():
    got = {n: d.audio.frame_count for n, d in pages().items() if d.audio is not None}
    assert got == FRAME_PAGES


def test_every_frame_is_the_synthetic_silent_frame():
    for n, d in pages().items():
        if d.audio is not None:
            assert set(frames_of(d)) == {SILENT}, n


def test_the_frames_decode_without_a_skip_to_digital_silence():
    for n, d in pages().items():
        if d.audio is not None:
            decoded = decode_opus(frames_of(d))
            assert decoded.skipped_frames == 0, n
            assert len(decoded.samples) > 0 and not np.any(decoded.samples), n


def test_the_script_would_change_no_fixture():
    assert SCRIPT.exists()
    assert load_script().main([str(FIX), "--check"]) == 0
