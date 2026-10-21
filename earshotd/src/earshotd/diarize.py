"""pyannote community-1 diarization from a local snapshot, and whisper-segment assignment."""

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import torch

from earshotd.audio import MODEL_RATE
from earshotd.transcribe import Segment

DEFAULT_THREADS = 8


@dataclass(frozen=True)
class Turn:
    start_ms: int
    end_ms: int
    speaker: str


@dataclass(frozen=True)
class Diarization:
    turns: list[Turn]  # overlapping speech kept, for scoring
    exclusive_turns: list[Turn]  # no overlap, for assigning transcript segments
    # speaker -> voice fingerprint (finite, non-zero); a speaker the model gave no usable one is
    # absent. None when the model produced none (distinct from {}, which means no usable one)
    embeddings: dict[str, np.ndarray] | None


def load_diarizer(model_dir: str | Path, threads: int = DEFAULT_THREADS):
    """Load the pipeline from a local directory; never from the Hub.

    `threads` sets torch's intra-op thread count for the whole process.

    A path that is not a directory would be read by pyannote as a Hub repo id, so it is refused.
    """
    from pyannote.audio import Pipeline

    path = Path(model_dir)
    if not path.is_dir():
        raise FileNotFoundError(f"diarization_model is not a directory: {path}")
    torch.set_num_threads(threads)
    pipeline = Pipeline.from_pretrained(str(path))
    if pipeline is None:
        raise RuntimeError(f"pyannote could not load a pipeline from {path}")
    pipeline.to(torch.device("cpu"))
    return pipeline


def _turns(annotation) -> list[Turn]:
    return [
        Turn(round(seg.start * 1000), round(seg.end * 1000), str(label))
        for seg, _track, label in annotation.itertracks(yield_label=True)
    ]


def diarize(pipeline, audio: np.ndarray) -> Diarization:
    """Diarize 16 kHz mono float32 audio; times are ms from the start of `audio`."""
    waveform = torch.from_numpy(np.ascontiguousarray(audio)).unsqueeze(0)
    out = pipeline({"waveform": waveform, "sample_rate": MODEL_RATE})
    labels = out.speaker_diarization.labels()
    embeddings = None
    if out.speaker_embeddings is not None:
        # pyannote orders the rows to match labels() (speaker_diarization.py, centroid reorder).
        if len(out.speaker_embeddings) != len(labels):
            raise ValueError(
                f"{len(out.speaker_embeddings)} embeddings for {len(labels)} speakers"
            )
        embeddings = {}
        for label, row in zip(labels, out.speaker_embeddings):
            vec = np.asarray(row, dtype=np.float32)
            # pyannote returns NaN for a speaker none of whose embeddings survive its clustering
            # filter, and zeros for one it could not cluster: no fingerprint, so no entry.
            if np.isfinite(vec).all() and np.linalg.norm(vec) > 0:
                embeddings[str(label)] = vec
    return Diarization(
        turns=_turns(out.speaker_diarization),
        exclusive_turns=_turns(out.exclusive_speaker_diarization),
        embeddings=embeddings,
    )


def assign_speakers(segments: list[Segment], turns: list[Turn]) -> list[str | None]:
    """For each segment, the speaker whose turns overlap it most; None when none overlap."""
    assigned: list[str | None] = []
    for seg in segments:
        overlap: dict[str, int] = {}
        for t in turns:
            ms = min(seg.end_ms, t.end_ms) - max(seg.start_ms, t.start_ms)
            if ms > 0:
                overlap[t.speaker] = overlap.get(t.speaker, 0) + ms
        assigned.append(max(overlap, key=lambda s: overlap[s]) if overlap else None)
    return assigned
