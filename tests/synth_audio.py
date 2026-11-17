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


VOWELS = {"a": (730, 1090, 2440), "i": (270, 2290, 3010), "u": (300, 870, 2240), "e": (530, 1840, 2480),
          "o": (570, 840, 2410)}


def voice(f0: float, tract: float, vowels: str, seconds: float = 6.0, seed: int = 0, sr: int = 16000) -> np.ndarray:
    """A synthetic talker: a glottal pulse train at about `f0` Hz (with drift and jitter) through
    three formant resonators per vowel, the formants scaled by `tract` (vocal tract length), with
    short pauses between syllables. Same f0 and tract, other vowels and seed: the same "speaker"."""
    from scipy.signal import lfilter
    rng = np.random.default_rng(seed)
    out = []
    per = seconds / len(vowels)
    for k, v in enumerate(vowels):
        n = int(per * 0.8 * sr)
        t = np.arange(n) / sr
        pitch = f0 * (1 + 0.08 * np.sin(2 * np.pi * (0.7 + 0.3 * rng.random()) * t + k)) \
            * (1 + 0.01 * rng.normal(size=n))
        phase = np.cumsum(pitch / sr)
        src = (np.diff(np.floor(phase), prepend=0) > 0).astype(np.float64) + 0.01 * rng.normal(size=n)
        x = src
        for f, bw in zip((np.array(VOWELS[v]) * tract), (80, 100, 120)):
            r = np.exp(-np.pi * bw / sr)
            x = lfilter([1.0], [1, -2 * r * np.cos(2 * np.pi * f / sr), r * r], x)
        x *= np.hanning(n)
        out += [x, np.zeros(int(per * 0.2 * sr))]
    y = np.concatenate(out)
    return (0.3 * y / np.abs(y).max()).astype(np.float32)
