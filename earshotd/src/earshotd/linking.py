"""Link a window's speakers to the day's speakers, and day speakers to enrolled voices.

A speaker is a voice fingerprint (pyannote's speaker embedding). `day_speakers.centroid` holds the
sum of the unit-length fingerprints seen for that speaker, each weighted by seconds of speech; only
its direction is ever read, so the sum needs no separate count.
"""

from __future__ import annotations

import numpy as np


def unit(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n else v


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(unit(a) @ unit(b))


def pack(v: np.ndarray) -> bytes:
    return np.asarray(v, dtype=np.float32).tobytes()


def unpack(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def match(
    wanted: dict[str, np.ndarray], known: dict[str, np.ndarray], threshold: float
) -> dict[str, str]:
    """One-to-one pairing of `wanted` keys to `known` keys, best similarity first.

    Only pairs with cosine similarity above `threshold` are made; two speakers of one window can
    never share a day speaker, so each known key is used at most once.
    """
    scored = sorted(
        ((cosine(w, k), wk, kk) for wk, w in wanted.items() for kk, k in known.items()),
        reverse=True,
    )
    pairs: dict[str, str] = {}
    taken: set[str] = set()
    for score, wk, kk in scored:
        if score <= threshold:
            break
        if wk in pairs or kk in taken:
            continue
        pairs[wk] = kk
        taken.add(kk)
    return pairs


def link_day(
    window: dict[str, np.ndarray],
    weights: dict[str, float],
    day: dict[str, np.ndarray],
    threshold: float,
) -> tuple[dict[str, str], dict[str, np.ndarray]]:
    """Map each window speaker to a day speaker label; return (mapping, updated day centroids).

    A speaker that matches no day speaker above `threshold` becomes the next `S<n>`. The returned
    centroids are only those the window touched.
    """
    mapping = match(window, day, threshold)
    top = max((int(k[1:]) for k in day if k[1:].isdigit()), default=0)
    for label in sorted(window):
        if label not in mapping:
            top += 1
            mapping[label] = f"S{top}"
    touched: dict[str, np.ndarray] = {}
    for label, vec in window.items():
        d = mapping[label]
        base = touched.get(d, day.get(d))
        add = unit(vec) * max(weights.get(label, 0.0), 0.1)
        touched[d] = add if base is None else base + add
    return mapping, touched
