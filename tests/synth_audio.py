"""Synthetic audio for tests (no recordings in the repo)."""
from __future__ import annotations

import numpy as np


def write_tone(path, seconds=3.0, sr=48000):
    import av
    t = np.arange(int(seconds * sr)) / sr
    rng = np.random.default_rng(0)
    x = 0.3 * np.sin(2 * np.pi * 220 * t) + 0.05 * rng.normal(size=t.shape)
    pcm = (x * 32767).astype(np.int16)
    with av.open(str(path), "w", format="ogg") as out:
        st = out.add_stream("libopus", rate=sr)
        st.layout = "mono"
        for i in range(0, len(pcm), 960):
            fr = av.AudioFrame.from_ndarray(pcm[None, i:i + 960], format="s16", layout="mono")
            fr.sample_rate = sr
            for p in st.encode(fr):
                out.mux(p)
        for p in st.encode(None):
            out.mux(p)
