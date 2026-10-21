"""A speaker fingerprint that is NaN or all-zero never leaves diarize.diarize.

Real model, real speech: 1.5 s of AMI ES2004a speech in 30 s of silence makes the real
community-1 pipeline return a NaN fingerprint (no embedding of the speaker survives its
clustering filter).
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import numpy as np
import pytest
import torch
from model_paths import SNAPSHOT
from test_diarization_ami import AUDIO_URL, fetch, load_audio

from earshotd import diarize
from earshotd.audio import MODEL_RATE

TOTAL_S, SPEECH_S, AT_S, FROM_S = 30, 1.5, 10, 30.0


def sparse_speech() -> np.ndarray:
    """30 s of silence holding 1.5 s of AMI speech (from 30.0 s of the recording) at 10 s."""
    source = load_audio(fetch(AUDIO_URL), int(FROM_S + SPEECH_S) + 1)
    speech = source[int(FROM_S * MODEL_RATE) : int((FROM_S + SPEECH_S) * MODEL_RATE)]
    audio = np.zeros(TOTAL_S * MODEL_RATE, dtype=np.float32)
    audio[AT_S * MODEL_RATE : AT_S * MODEL_RATE + len(speech)] = speech
    return audio


@pytest.fixture(scope="module")
def pipeline():
    return diarize.load_diarizer(SNAPSHOT)


@pytest.mark.model("diarization")
def test_no_fingerprint_is_non_finite_or_zero(pipeline):
    audio = sparse_speech()
    wave = torch.from_numpy(np.ascontiguousarray(audio)).unsqueeze(0)
    raw = pipeline({"waveform": wave, "sample_rate": MODEL_RATE})
    # The real model returns a NaN row for this audio: the filter has to be ours.
    assert np.isnan(np.asarray(raw.speaker_embeddings)).any()

    result = diarize.diarize(pipeline, audio)
    assert result.embeddings is not None
    for speaker, vec in result.embeddings.items():
        assert np.isfinite(vec).all(), speaker
        assert np.linalg.norm(vec) > 0, speaker
    nan_labels = {
        str(label)
        for label, vec in zip(raw.speaker_diarization.labels(), raw.speaker_embeddings)
        if not np.isfinite(vec).all()
    }
    assert nan_labels
    assert not nan_labels.intersection(result.embeddings)


class _Ann:
    def __init__(self, labels):
        self._labels = labels

    def labels(self):
        return self._labels

    def itertracks(self, yield_label=True):
        return iter(())


class _Out:
    speaker_diarization = _Ann(["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"])
    exclusive_speaker_diarization = _Ann([])
    speaker_embeddings = np.array(
        [[1.0, 0.0], [float("nan"), 1.0], [0.0, 0.0]], dtype=np.float32
    )


def test_nan_and_zero_rows_are_dropped_and_a_normal_row_kept():
    result = diarize.diarize(lambda _inputs: _Out(), np.zeros(MODEL_RATE, np.float32))
    assert result.embeddings is not None
    assert set(result.embeddings) == {"SPEAKER_00"}
