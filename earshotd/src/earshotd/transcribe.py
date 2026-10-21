"""faster-whisper transcription of one window of audio."""

from dataclasses import dataclass

import numpy as np
from faster_whisper import WhisperModel


@dataclass(frozen=True)
class Segment:
    start_ms: int
    end_ms: int
    text: str


def load_model(model: str) -> WhisperModel:
    """`model` is a faster-whisper model name or a local CTranslate2 directory."""
    return WhisperModel(model, device="cpu", compute_type="int8")


def transcribe(model: WhisperModel, audio: np.ndarray) -> list[Segment]:
    """Transcribe 16 kHz mono float32 audio; times are ms from the start of `audio`."""
    segments, _info = model.transcribe(
        audio,
        language="en",
        vad_filter=True,
        condition_on_previous_text=False,
    )
    return [
        Segment(round(s.start * 1000), round(s.end * 1000), s.text.strip())
        for s in segments
        if s.text.strip()
    ]
