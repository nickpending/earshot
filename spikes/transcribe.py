import struct, wave
import av, numpy as np

frames = []
with open("frames.bin", "rb") as f:
    while hdr := f.read(14):
        pi, ts, ln = struct.unpack("<IQH", hdr)
        frames.append((pi, ts, f.read(ln)))

ctx = av.CodecContext.create("opus", "r")
ctx.sample_rate, ctx.layout = 16000, "mono"
rs = av.AudioResampler(format="flt", layout="mono", rate=16000)
pcm, bad = [], 0
for _, _, data in frames:
    try:
        for fr in ctx.decode(av.Packet(data)):
            for o in rs.resample(fr):
                pcm.append(o.to_ndarray().reshape(-1).astype(np.float32))
    except av.error.InvalidDataError:
        bad += 1
audio = np.concatenate(pcm)
if audio.dtype != np.float32 or np.abs(audio).max() > 1.5:
    audio = audio / 32768.0
print(f"frames {len(frames)} bad {bad} seconds {len(audio) / 16000:.1f} peak {np.abs(audio).max():.3f}")
with wave.open("pendant.wav", "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
    w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())

import mlx_whisper
r = mlx_whisper.transcribe(audio, path_or_hf_repo="mlx-community/whisper-large-v3-turbo", language="en", condition_on_previous_text=False, no_speech_threshold=0.6)
for s in r["segments"]:
    print(f"[{s['start']:6.1f}-{s['end']:6.1f}] {s['text'].strip()}")
