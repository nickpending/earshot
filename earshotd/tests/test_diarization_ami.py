"""C7: first 10 minutes of AMI ES2004a through earshotd's diarization step as one window.

Real speakers only; synthetic TTS voices cannot test separation. The audio and the reference
are downloaded into a cache directory (never committed: *.wav is gitignored).
"""

import os
import urllib.request
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import av
import numpy as np
import pytest
from model_paths import SNAPSHOT
from pyannote.core import Annotation, Segment, Timeline
from pyannote.metrics.diarization import DiarizationErrorRate

from earshotd import diarize
from earshotd.audio import MODEL_RATE

AUDIO_URL = "https://groups.inf.ed.ac.uk/ami/AMICorpusMirror/amicorpus/ES2004a/audio/ES2004a.Mix-Headset.wav"
RTTM_URL = "https://raw.githubusercontent.com/pyannote/AMI-diarization-setup/main/only_words/rttms/test/ES2004a.rttm"
WINDOW_S = 600
pytestmark = pytest.mark.model("diarization")
CACHE = Path(
    os.environ.get("EARSHOT_TEST_CACHE", "~/.cache/earshotd-tests")
).expanduser()


def fetch(url: str) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / url.rsplit("/", 1)[1]
    if not path.exists():
        tmp = path.with_suffix(path.suffix + ".part")
        urllib.request.urlretrieve(url, tmp)
        tmp.rename(path)
    return path


def load_audio(path: Path, seconds: int) -> np.ndarray:
    """First `seconds` of a wav as 16 kHz mono float32."""
    resampler = av.AudioResampler(format="flt", layout="mono", rate=MODEL_RATE)
    chunks, total = [], 0
    with av.open(str(path)) as container:
        for frame in container.decode(audio=0):
            for out in resampler.resample(frame):
                chunk = out.to_ndarray().reshape(-1)
                chunks.append(chunk)
                total += len(chunk)
            if total >= seconds * MODEL_RATE:
                break
    return np.concatenate(chunks)[: seconds * MODEL_RATE].astype(np.float32)


def load_reference(path: Path, seconds: int) -> Annotation:
    ref = Annotation()
    for line in path.read_text().splitlines():
        f = line.split()
        if not f or f[0] != "SPEAKER":
            continue
        start, dur, speaker = float(f[3]), float(f[4]), f[7]
        if start < seconds:
            ref[Segment(start, min(start + dur, seconds))] = speaker
    return ref


def to_annotation(turns: list[diarize.Turn]) -> Annotation:
    hyp = Annotation()
    for t in turns:
        hyp[Segment(t.start_ms / 1000, t.end_ms / 1000)] = t.speaker
    return hyp


@pytest.fixture(scope="module")
def ami():
    audio = load_audio(fetch(AUDIO_URL), WINDOW_S)
    reference = load_reference(fetch(RTTM_URL), WINDOW_S)
    return audio, reference


@pytest.fixture(scope="module")
def pipeline():
    return diarize.load_diarizer(SNAPSHOT)


@pytest.fixture(scope="module")
def result(ami, pipeline):
    audio, _reference = ami
    return diarize.diarize(pipeline, audio)


def test_single_window_der_and_confusion(ami, result):
    audio, reference = ami
    assert len(audio) == WINDOW_S * MODEL_RATE
    assert len(reference.labels()) == 4

    metric = DiarizationErrorRate(collar=0.25, skip_overlap=False)
    uem = Timeline([Segment(0, WINDOW_S)])
    metric(reference, to_annotation(result.turns), uem=uem)
    parts = metric[:]
    total = parts["total"]
    der = abs(metric)
    confusion = parts["confusion"] / total
    print(f"DER {der:.3f} confusion {confusion:.3f} of {total:.1f}s speech")

    assert der <= 0.20
    assert confusion <= 0.05


def test_each_speaker_embedding_is_the_one_for_that_speakers_own_audio(ami, result):
    """Real pyannote rows follow labels() order: a speaker's own speech embeds nearest its row."""
    import torch
    from pyannote.audio import Inference, Model

    audio, _reference = ami
    embed = Inference(
        Model.from_pretrained(str(SNAPSHOT / "embedding")), window="whole"
    )
    assert result.embeddings is not None
    speakers = sorted({t.speaker for t in result.exclusive_turns})
    assert len(speakers) >= 3 and set(result.embeddings) == set(speakers)

    def unit(v: np.ndarray) -> np.ndarray:
        return v / np.linalg.norm(v)

    for speaker in speakers:
        turns = sorted(
            (t for t in result.exclusive_turns if t.speaker == speaker),
            key=lambda t: t.start_ms - t.end_ms,
        )
        pieces, total = [], 0
        for t in turns:  # longest turns first, up to 60 s
            pieces.append(
                audio[t.start_ms * MODEL_RATE // 1000 : t.end_ms * MODEL_RATE // 1000]
            )
            total += t.end_ms - t.start_ms
            if total >= 60_000:
                break
        wave = torch.from_numpy(np.concatenate(pieces)).reshape(1, -1)
        own = unit(np.asarray(embed({"waveform": wave, "sample_rate": MODEL_RATE})))
        scores = {
            other: float(own @ unit(vec)) for other, vec in result.embeddings.items()
        }
        assert max(scores, key=lambda k: scores[k]) == speaker, scores
