"""Opus frames to the audio handed to the models: 16 kHz mono float32."""

import logging
from typing import NamedTuple

import av
import numpy as np

DECODER_RATE = (
    48000  # libopus via PyAV outputs 48 kHz whatever the stream was encoded at
)
MODEL_RATE = 16000

log = logging.getLogger("earshotd.audio")


class DecodedAudio(NamedTuple):
    samples: np.ndarray  # 16 kHz mono float32
    skipped_frames: int  # frames the decoder rejected; non-zero means a degraded window


def decode_opus(frames: list[bytes]) -> DecodedAudio:
    """Decode opus frames in order and resample the 48 kHz output to 16 kHz mono float32.

    A frame the decoder rejects is skipped and counted in `skipped_frames`, and a warning is
    logged; the rest of the window still decodes.
    """
    ctx = av.CodecContext.create("opus", "r")
    resampler = av.AudioResampler(format="flt", layout="mono", rate=MODEL_RATE)
    chunks: list[np.ndarray] = []
    skipped = 0
    for data in frames:
        try:
            decoded = ctx.decode(av.Packet(data))
        except av.error.InvalidDataError:
            skipped += 1
            continue
        for frame in decoded:
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
    for out in resampler.resample(None):
        chunks.append(out.to_ndarray().reshape(-1))
    if skipped:
        log.warning("decode_opus skipped %d of %d frames", skipped, len(frames))
    if not chunks:
        return DecodedAudio(np.zeros(0, dtype=np.float32), skipped)
    # Opus decoding overshoots full scale on loud speech (peak 1.45 on the fixture).
    samples = np.clip(np.concatenate(chunks), -1.0, 1.0).astype(np.float32, copy=False)
    return DecodedAudio(samples, skipped)
