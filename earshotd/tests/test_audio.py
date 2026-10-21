"""C6, decode half: the fixture's opus frames become 16 kHz mono float32."""

import logging
import os
import struct
from pathlib import Path

import av
import numpy as np

os.environ.setdefault("HF_HUB_OFFLINE", "1")

from earshotd.audio import DECODER_RATE, MODEL_RATE, decode_opus

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "say-test.pages.v1"


def fixture_frames() -> list[bytes]:
    body = FIXTURE.read_bytes()
    frames: list[bytes] = []
    pos = 0
    while pos < len(body):
        _session, _index, _ts, count = struct.unpack_from("<IIQH", body, pos)
        pos += 18
        for _ in range(count):
            (length,) = struct.unpack_from("<H", body, pos)
            pos += 2
            frames.append(body[pos : pos + length])
            pos += length
    return frames


def test_fixture_yields_2400_frames():
    assert len(fixture_frames()) == 2400


def test_decode_is_16k_mono_float32_of_the_right_length():
    audio = decode_opus(fixture_frames()).samples
    assert isinstance(audio, np.ndarray)
    assert audio.dtype == np.float32
    assert audio.ndim == 1
    assert abs(len(audio) / MODEL_RATE - 48.0) <= 0.1
    assert 0.01 < float(np.abs(audio).max()) <= 1.0


def test_decoder_really_outputs_48k_and_we_resample():
    ctx = av.CodecContext.create("opus", "r")
    out = ctx.decode(av.Packet(fixture_frames()[0]))
    assert out and out[0].sample_rate == 48000 == DECODER_RATE
    assert MODEL_RATE == 16000


def test_empty_input_is_empty_audio():
    result = decode_opus([])
    assert result.samples.dtype == np.float32 and len(result.samples) == 0
    assert result.skipped_frames == 0


def test_a_corrupt_frame_is_skipped_and_reported(caplog):
    frames = fixture_frames()[:100]
    clean = decode_opus(frames)
    assert clean.skipped_frames == 0
    with caplog.at_level(logging.WARNING, logger="earshotd.audio"):
        with_bad = decode_opus(frames[:50] + [b"\xff"] + frames[50:])
    assert with_bad.skipped_frames == 1
    assert abs(len(with_bad.samples) - len(clean.samples)) <= MODEL_RATE * 0.05
    assert any("skipped 1 of 101" in r.getMessage() for r in caplog.records)
