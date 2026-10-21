"""C6, model half: whisper and pyannote are configured as specified and load locally."""

import os
import socket

import numpy as np
import pytest
from faster_whisper import WhisperModel
from faster_whisper.utils import download_model

os.environ.setdefault("HF_HUB_OFFLINE", "1")

from model_paths import SNAPSHOT
from test_audio import fixture_frames

from earshotd import diarize, transcribe
from earshotd.audio import decode_opus


@pytest.fixture(scope="module")
def audio():
    return decode_opus(fixture_frames()).samples


@pytest.fixture(scope="module")
def whisper_dir():
    return download_model("large-v3-turbo", local_files_only=True)


@pytest.fixture
def no_network(monkeypatch):
    def refuse(self, *args, **kwargs):
        raise AssertionError(f"network connection attempted: {args}")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)


@pytest.mark.model("whisper")
def test_whisper_is_cpu_int8_and_transcribe_uses_the_specified_options(
    monkeypatch, whisper_dir, audio, no_network
):
    ctor, call = {}, {}
    real_init, real_transcribe = WhisperModel.__init__, WhisperModel.transcribe

    def init(self, *args, **kwargs):
        ctor.update(args=args, kwargs=kwargs)
        real_init(self, *args, **kwargs)

    def spy(self, audio, **kwargs):
        call.update(kwargs, audio=audio)
        return real_transcribe(self, audio, **kwargs)

    monkeypatch.setattr(WhisperModel, "__init__", init)
    monkeypatch.setattr(WhisperModel, "transcribe", spy)

    model = transcribe.load_model(whisper_dir)
    segments = transcribe.transcribe(model, audio)

    assert ctor["args"] == (whisper_dir,)
    assert ctor["kwargs"]["device"] == "cpu"
    assert ctor["kwargs"]["compute_type"] == "int8"
    assert call["language"] == "en"
    assert call["vad_filter"] is True
    assert call["condition_on_previous_text"] is False
    assert isinstance(call["audio"], np.ndarray) and call["audio"].dtype == np.float32

    text = " ".join(s.text for s in segments).lower()
    assert "quick brown fox" in text
    assert segments and all(0 <= s.start_ms < s.end_ms <= 48_100 for s in segments)
    assert all(isinstance(s.start_ms, int) for s in segments)


@pytest.mark.model("diarization")
def test_diarizer_loads_from_the_local_snapshot_with_no_token_and_no_network(
    audio, no_network
):
    assert SNAPSHOT.is_dir(), f"snapshot missing at {SNAPSHOT}"
    pipeline = diarize.load_diarizer(SNAPSHOT)
    result = diarize.diarize(pipeline, audio)
    assert result.turns
    assert all(0 <= t.start_ms < t.end_ms <= 48_100 for t in result.turns)
    speakers = {t.speaker for t in result.turns}
    assert result.embeddings is not None
    assert set(result.embeddings) == speakers
    for emb in result.embeddings.values():
        assert emb.ndim == 1 and emb.dtype == np.float32 and np.isfinite(emb).all()


def test_diarizer_refuses_a_missing_directory_instead_of_asking_the_hub(tmp_path):
    with pytest.raises(FileNotFoundError):
        diarize.load_diarizer(tmp_path / "pyannote" / "speaker-diarization-community-1")


def test_assign_picks_the_speaker_with_most_overlap():
    turns = [
        diarize.Turn(0, 4000, "A"),
        diarize.Turn(4000, 10_000, "B"),
    ]
    segs = [
        transcribe.Segment(500, 3500, "one"),  # all A
        transcribe.Segment(3000, 8000, "two"),  # 1000 A, 4000 B
        transcribe.Segment(20_000, 21_000, "three"),  # no turn
    ]
    assert diarize.assign_speakers(segs, turns) == ["A", "B", None]


class _FakeAnnotation:
    def __init__(self, labels):
        self._labels = labels

    def labels(self):
        return self._labels

    def itertracks(self, yield_label=False):
        return iter(())


class _FakeOutput:
    def __init__(self, labels, embeddings):
        self.speaker_diarization = _FakeAnnotation(labels)
        self.exclusive_speaker_diarization = _FakeAnnotation(labels)
        self.speaker_embeddings = embeddings


def test_embeddings_pair_with_labels_by_row_order():
    rows = np.array([[1.0, 0.0], [0.0, 1.0], [2.0, 2.0]])
    out = diarize.diarize(
        lambda _in: _FakeOutput(["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"], rows),
        np.zeros(16000, dtype=np.float32),
    )
    assert out.embeddings is not None
    assert out.embeddings["SPEAKER_01"].tolist() == [0.0, 1.0]
    assert out.embeddings["SPEAKER_02"].tolist() == [2.0, 2.0]


def test_missing_embeddings_are_none_not_an_empty_dict():
    out = diarize.diarize(
        lambda _in: _FakeOutput(["SPEAKER_00"], None),
        np.zeros(16000, dtype=np.float32),
    )
    assert out.embeddings is None


def test_no_speakers_is_an_empty_dict_not_none():
    out = diarize.diarize(
        lambda _in: _FakeOutput([], np.zeros((0, 2))),
        np.zeros(16000, dtype=np.float32),
    )
    assert out.embeddings == {}


def test_embedding_row_count_must_match_labels():
    with pytest.raises(ValueError):
        diarize.diarize(
            lambda _in: _FakeOutput(["A", "B"], np.zeros((1, 2))),
            np.zeros(16000, dtype=np.float32),
        )


@pytest.mark.model("diarization")
def test_thread_count_is_a_parameter(tmp_path):
    import torch

    before = torch.get_num_threads()
    try:
        diarize.load_diarizer(SNAPSHOT, threads=3)
        assert torch.get_num_threads() == 3
    finally:
        torch.set_num_threads(before)
