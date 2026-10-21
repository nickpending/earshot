"""C8: two windows of AMI ES2004a share one set of day-level speaker labels.

Real speakers only. The first 10 minutes are encoded to opus pages the way the pendant sends
them, stored, and split at a silence near 5:00 into two windows of the same day that run through
the pipeline (real PyAV, faster-whisper and pyannote). The enrolment of C9 happens between the
two windows, so test_voice_enroll_ami.py reads the same run.
"""

import functools
import os
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import av
import numpy as np
import pytest
from faster_whisper.utils import download_model
from model_paths import SNAPSHOT
from pyannote.core import Annotation, Segment, Timeline
from pyannote.metrics.diarization import DiarizationErrorRate
from test_diarization_ami import (
    AUDIO_URL,
    RTTM_URL,
    WINDOW_S,
    fetch,
    load_audio,
    load_reference,
    to_annotation,
)

from earshotd import cli, diarize, store
from earshotd.audio import MODEL_RATE
from earshotd.config import Config
from earshotd.diarize import Turn
from earshotd.pages import Page
from earshotd.pipeline import LocalModels, Pipeline, StoredPage, WindowResult
from earshotd.windowing import FRAME_MS, split_frames

BASE = 1_790_896_000_000  # 2026-10-01, local time
FRAMES_PER_PAGE = 75
FRAME_SAMPLES = MODEL_RATE * FRAME_MS // 1000
SPLIT_NEAR_S = 300


def encode_opus(audio: np.ndarray) -> list[bytes]:
    """16 kHz mono float32 to 20 ms opus frames, as the pendant produces them."""
    ctx = av.CodecContext.create("libopus", "w")
    ctx.sample_rate = MODEL_RATE
    ctx.layout = "mono"
    ctx.format = "s16"
    ctx.bit_rate = 24000
    ctx.options = {"frame_duration": "20"}
    ctx.open()
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    packets: list[bytes] = []
    for n in range(len(pcm) // FRAME_SAMPLES):
        frame = av.AudioFrame.from_ndarray(
            pcm[n * FRAME_SAMPLES : (n + 1) * FRAME_SAMPLES].reshape(1, -1),
            format="s16",
            layout="mono",
        )
        frame.sample_rate = MODEL_RATE
        frame.pts = n * FRAME_SAMPLES
        packets += [bytes(p) for p in ctx.encode(frame)]
    packets += [bytes(p) for p in ctx.encode(None)]
    return packets


def silence_near(reference: Annotation, near_s: float) -> float:
    """Middle of the reference's non-speech gap nearest `near_s` (at least one second long)."""
    speech = reference.get_timeline().support()
    gaps = [
        (a.end, b.start)
        for a, b in zip(speech, list(speech)[1:])
        if b.start - a.end >= 1.0
    ]
    s, e = min(gaps, key=lambda g: abs((g[0] + g[1]) / 2 - near_s))
    return (s + e) / 2


def pages_of(frames: list[bytes], first: int, last: int, first_page: int) -> list[Page]:
    out = []
    for n, at in enumerate(range(first, last, FRAMES_PER_PAGE)):
        chunk = frames[at : min(at + FRAMES_PER_PAGE, last)]
        blob = b"".join(struct.pack("<H", len(f)) + f for f in chunk)
        out.append(Page(1, first_page + n, BASE + at * FRAME_MS, blob, len(chunk)))
    return out


def stored(pages: list[Page]) -> list[StoredPage]:
    return [
        StoredPage(p.session, p.page_index, p.page_ts_ms, split_frames(p.frames))
        for p in pages
    ]


def enrolment_range(
    reference: Annotation, speaker: str, before_s: float
) -> tuple[float, float]:
    """The longest stretch before `before_s` where only `speaker` talks."""
    own = [s for s, _t, lab in reference.itertracks(yield_label=True) if lab == speaker]
    others = Timeline(
        [s for s, _t, lab in reference.itertracks(yield_label=True) if lab != speaker]
    )
    runs: list[list[float]] = []
    for s in sorted(own):
        if s.end > before_s:
            continue
        if runs and s.start - runs[-1][1] < 1.0:
            runs[-1][1] = s.end
        else:
            runs.append([s.start, s.end])
    clean = [
        (a, b) for a, b in runs if not others.crop(Segment(a, b), mode="intersection")
    ]
    a, b = max(clean, key=lambda r: r[1] - r[0])
    return a, b


@dataclass
class AmiRun:
    config: Config
    reference: Annotation
    split_s: float
    first: WindowResult
    second: WindowResult
    enrolled: tuple[float, float]
    single_window_turns: list[Turn]
    scratch: tempfile.TemporaryDirectory


@functools.cache
def ami_run() -> AmiRun:
    audio = load_audio(fetch(AUDIO_URL), WINDOW_S)
    reference = load_reference(fetch(RTTM_URL), WINDOW_S)
    split_s = silence_near(reference, SPLIT_NEAR_S)
    split_frame = round(split_s * 1000 / FRAME_MS)
    total_frames = len(audio) // FRAME_SAMPLES

    scratch = tempfile.TemporaryDirectory(prefix="earshotd-ami-")
    config = Config(
        tokens={},
        data_dir=Path(scratch.name),
        whisper_model=download_model("large-v3-turbo", local_files_only=True),
        diarization_model=str(SNAPSHOT),
    )
    store.init(config.db_path)
    frames = encode_opus(audio)
    assert len(frames) >= total_frames - 2
    one = pages_of(frames, 0, split_frame, 0)
    two = pages_of(frames, split_frame, min(len(frames), total_frames), len(one))
    store.store_pages(config.db_path, one + two)

    models = LocalModels(config)
    pipeline = Pipeline(config, models)
    first = pipeline.process_window(stored(one))

    a, b = enrolment_range(reference, "FEE013", split_s)
    code = cli.main(
        [
            "voice", "add", "FEE013",
            "--from", str(BASE + round(a * 1000)),
            "--to", str(BASE + round(b * 1000)),
        ],
        config=config,
        models=models,
    )  # fmt: skip
    assert code == 0

    second = pipeline.process_window(stored(two))
    single = diarize.diarize(models._diarizer, audio).turns
    return AmiRun(config, reference, split_s, first, second, (a, b), single, scratch)


def day_annotation(run: AmiRun) -> Annotation:
    hyp = Annotation()
    for result in (run.first, run.second):
        offset = (result.first_ms - BASE) / 1000
        for t in result.day_turns:
            hyp[Segment(offset + t.start_ms / 1000, offset + t.end_ms / 1000)] = (
                t.speaker
            )
    return hyp


def confusion(reference, hyp):
    metric = DiarizationErrorRate(collar=0.25, skip_overlap=False)
    metric(reference, hyp, uem=Timeline([Segment(0, WINDOW_S)]))
    parts = metric[:]
    return parts["confusion"] / parts["total"]


@pytest.mark.model("diarization", "whisper")
def test_split_window_confusion_is_within_0_03_of_the_single_window():
    run = ami_run()
    assert abs(run.split_s - SPLIT_NEAR_S) < 15
    single = confusion(run.reference, to_annotation(run.single_window_turns))
    split = confusion(run.reference, day_annotation(run))
    print(f"confusion single {single:.3f} split {split:.3f}")
    assert split <= single + 0.03


@pytest.mark.model("diarization", "whisper")
def test_each_reference_speaker_maps_to_one_day_label():
    run = ami_run()
    hyp = day_annotation(run)
    metric = DiarizationErrorRate(collar=0.25, skip_overlap=False)
    uem = Timeline([Segment(0, WINDOW_S)])
    # optimal_mapping maps hypothesis labels to reference labels.
    mapping = {
        ref: label
        for label, ref in metric.optimal_mapping(run.reference, hyp, uem=uem).items()
    }
    assert (
        set(mapping) == set(run.reference.labels()) and len(run.reference.labels()) == 4
    )
    assert len(set(mapping.values())) == 4, mapping


@pytest.mark.model("diarization", "whisper")
def test_day_labels_are_shared_between_the_windows():
    run = ami_run()
    first = {s.speaker for s in run.first.segments if s.speaker}
    second = {s.speaker for s in run.second.segments if s.speaker}
    assert len(first) >= 3 and len(first & second) >= 3
    # A speaker new in the second window gets the next label, never a first-window one.
    assert not (second - first) & {"S1", "S2", "S3"}


def test_linking_pairs_one_to_one_above_the_threshold():
    from earshotd import linking

    a, b = np.array([1, 0, 0], dtype=np.float32), np.array([0, 1, 0], dtype=np.float32)
    near_a = np.array([0.9, 0.1, 0], dtype=np.float32)
    day = {"S1": a}
    mapping, touched = linking.link_day(
        {"x": near_a, "y": b}, {"x": 5.0, "y": 2.0}, day, 0.5
    )
    assert mapping == {"x": "S1", "y": "S2"}
    assert set(touched) == {"S1", "S2"}
    # Two window speakers never share one day speaker, even when both are close to it.
    both, _ = linking.link_day({"x": near_a, "y": a}, {}, day, 0.5)
    assert sorted(both.values()) == ["S1", "S2"]
