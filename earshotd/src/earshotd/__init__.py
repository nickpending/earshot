"""earshotd: the earshot audio store, pipeline and read API."""

import os

# Telemetry off unless the operator opts in, set here before any earshotd module loads a library:
# pyannote.audio reports every diarization (audio length, speaker count, a session id) to
# otel.pyannote.ai; onnxruntime, loaded by faster-whisper's VAD, posts to Microsoft's OneCollector from
# native code; huggingface_hub reads its switch once, at import.
os.environ.setdefault("PYANNOTE_METRICS_ENABLED", "false")
os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
