"""Near-field "wearer" signal: is this turn the person wearing the pendant?

The wearer's mouth is a few centimetres from the microphone; everyone else is a metre or
more away. Per speaker turn (speakers.rescore_turns) three acoustic features say how close
the voice is:

  level_db   the turn's speech level (75th percentile of 10 ms frame energy over its words)
             above the conversation's noise floor (10th percentile over the whole clip)
  decay_db   how fast energy falls after a syllable: the 5th percentile of frame-to-frame
             drops over the turn, counting only frames well above the floor. Room echo
             smears offsets, so far voices decay slowly (small values); a dry near voice
             drops steeply.
  flatness   median log10 spectral flatness of the turn's loudest frames (100 Hz-4 kHz):
             reverberant, low-SNR speech is flatter (closer to noise)

plus the turn's score for the wearer from the configured scorer (`speakers.scoring`),
as its margin over that person's threshold.

A small logistic model over those four, fitted from labelled turns by
`python -m pendant_direct.eval_speakers --fit-wearer`, lives in config.json as
`speakers.wearer_model`. The acoustic features are z-scored per pendant (mic gain and
self-noise differ), with each pendant's own mean and spread; a pendant the model has
never seen uses the stats of its own stored turns (`speaker_turns`) once it has enough,
else the pooled stats. Without a model nothing changes.

numpy only.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

SAMPLE_RATE = 16_000
FRAME = 160                 # 10 ms energy frames
FFT = 512                   # 32 ms spectral frames
FLOOR_PCT = 10              # the conversation's noise floor: this percentile of frame energy...
FLOOR_MIN_DB = -70.0        # ...over frames that aren't digital silence, never below this
LEVEL_PCT = 75
DECAY_PCT = 5
DECAY_ABOVE_DB = 10.0       # a drop counts only from a frame this far above the floor
MIN_FRAMES = 20             # a turn with fewer voiced frames has no features
MAX_FLAT_FRAMES = 200
ACOUSTIC = ("level_db", "decay_db", "flatness")
FEATURES = ACOUSTIC + ("score",)
MIN_DEVICE_TURNS = 100      # stored turns a pendant needs before its own stats replace the pooled ones
L2 = 1.0


def frame_db(audio: np.ndarray) -> np.ndarray:
    """Energy of each 10 ms frame, in dB."""
    x = np.asarray(audio, dtype=np.float32)
    n = len(x) // FRAME
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    e = np.square(x[:n * FRAME].reshape(n, FRAME), dtype=np.float32).mean(axis=1)
    return (10.0 * np.log10(e + 1e-10)).astype(np.float32)


def noise_floor(db: np.ndarray) -> float:
    """FLOOR_PCT of the frames with any signal (Opus DTX and gaps decode to exact zeros,
    which would sink the floor to -100 dB), clamped to FLOOR_MIN_DB."""
    live = db[db > -99.0]
    return max(float(np.percentile(live, FLOOR_PCT)), FLOOR_MIN_DB) if len(live) else FLOOR_MIN_DB


_BAND = None


def _flatness(audio: np.ndarray, frames: np.ndarray) -> float:
    """Median log10 spectral flatness of FFT-sized frames starting at these 10 ms frames."""
    global _BAND
    if _BAND is None:
        f = np.fft.rfftfreq(FFT, 1 / SAMPLE_RATE)
        _BAND = ((f >= 100) & (f <= 4000), np.hanning(FFT).astype(np.float32))
    band, win = _BAND
    starts = frames * FRAME
    starts = starts[starts + FFT <= len(audio)]
    if len(starts) > MAX_FLAT_FRAMES:
        starts = starts[np.linspace(0, len(starts) - 1, MAX_FLAT_FRAMES).astype(int)]
    if not len(starts):
        return float("nan")
    seg = np.stack([audio[s:s + FFT] for s in starts]) * win
    p = np.abs(np.fft.rfft(seg, axis=1))[:, band] ** 2 + 1e-12
    flat = np.exp(np.log(p).mean(axis=1)) / p.mean(axis=1)
    return float(np.median(np.log10(flat)))


def turn_features(audio: np.ndarray, db: np.ndarray, floor: float, words) -> tuple | None:
    """(level_db, decay_db, flatness) of one turn from its words [(start_s, end_s, ...)], or
    None when it has too little voiced audio."""
    if not len(db) or not words:
        return None
    n = len(db)
    # only the turn's own span of frames: cost follows the turn, not the clip
    a = max(0, min(n, int(min(w[0] for w in words) * SAMPLE_RATE / FRAME)))
    b = max(a, min(n, int(math.ceil(max(w[1] for w in words) * SAMPLE_RATE / FRAME)) + 10))
    span = db[a:b]
    voiced = np.zeros(len(span), dtype=bool)
    for w in words:
        s, e = int(w[0] * SAMPLE_RATE / FRAME) - a, int(math.ceil(w[1] * SAMPLE_RATE / FRAME)) - a
        voiced[max(0, s):max(0, min(len(span), e))] = True
    if voiced.sum() < MIN_FRAMES:
        return None
    vdb = span[voiced]
    level = float(np.percentile(vdb, LEVEL_PCT)) - floor
    # decays over the turn's whole span (offsets fall into the gaps between words)
    d = np.diff(span)
    from_speech = span[:-1] > floor + DECAY_ABOVE_DB
    decay = float(-np.percentile(d[from_speech], DECAY_PCT)) if from_speech.sum() >= MIN_FRAMES else 0.0
    loud = a + np.flatnonzero(voiced & (span >= np.percentile(vdb, 50)))
    flat = _flatness(audio, loud)
    if not np.isfinite(flat):
        return None
    return level, decay, flat


def clip_features(audio: np.ndarray, turn_words: list) -> list[tuple | None]:
    """turn_features for each turn's words, against this clip's noise floor."""
    audio = np.asarray(audio, dtype=np.float32)
    db = frame_db(audio)
    floor = noise_floor(db)
    return [turn_features(audio, db, floor, w) for w in turn_words]


def sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -40, 40)))


def stats(rows) -> dict:
    """{"mean", "std", "n"} of acoustic feature rows [(level, decay, flat)]."""
    x = np.asarray(rows, dtype=np.float64).reshape(-1, len(ACOUSTIC))
    if not len(x):
        return {"mean": [0.0] * len(ACOUSTIC), "std": [1.0] * len(ACOUSTIC), "n": 0}
    return {"mean": [float(v) for v in x.mean(0)], "std": [float(max(v, 1e-6)) for v in x.std(0)],
            "n": int(len(x))}


@dataclass
class Model:
    """Logistic wearer-vs-not over (per-pendant z-scored acoustic features, standardized
    wearer score)."""
    coef: list
    bias: float
    scoring: str
    score_mean: float
    score_std: float
    devices: dict = field(default_factory=dict)     # {device: stats()}
    pooled: dict = field(default_factory=dict)
    info: dict = field(default_factory=dict)        # fit report (counts, held-out accuracy)

    def norm(self, device, extra: dict | None = None) -> dict:
        """The acoustic stats used for this pendant: its own from the fit, else `extra` (its
        stored turns, when there are enough), else the pooled ones."""
        if device in self.devices:
            return self.devices[device]
        if extra and extra.get("n", 0) >= MIN_DEVICE_TURNS:
            return extra
        return self.pooled

    def logit(self, feats, score: float, st: dict) -> float:
        z = (np.asarray(feats, dtype=np.float64) - st["mean"]) / np.asarray(st["std"])
        s = (score - self.score_mean) / max(self.score_std, 1e-6)
        return float(np.dot(self.coef, [*z, s]) + self.bias)

    def to_config(self) -> dict:
        return {"features": list(FEATURES), "coef": [round(float(c), 6) for c in self.coef],
                "bias": round(float(self.bias), 6), "scoring": self.scoring,
                "score": {"mean": round(self.score_mean, 6), "std": round(self.score_std, 6)},
                "devices": self.devices, "pooled": self.pooled, **({"fit": self.info} if self.info else {})}

    @classmethod
    def from_config(cls, d) -> Model | None:
        """The model in `speakers.wearer_model`, or None when absent or malformed."""
        if not isinstance(d, dict) or not isinstance(d.get("features"), (list, tuple)) \
                or list(d["features"]) != list(FEATURES):
            return None

        def finite(xs):
            xs = [float(v) for v in xs]
            if not all(map(math.isfinite, xs)):
                raise ValueError
            return xs

        def st(s):
            m, sd = finite(s["mean"]), finite(s["std"])
            if len(m) != len(ACOUSTIC) or len(sd) != len(ACOUSTIC) or min(sd) <= 0:
                raise ValueError
            return {"mean": m, "std": sd, "n": int(s.get("n", 0))}
        try:
            if not isinstance(d.get("coef"), (list, tuple)) or not isinstance(d.get("score"), dict) \
                    or not isinstance(d.get("pooled"), dict) or not isinstance(d.get("devices") or {}, dict):
                return None
            coef = finite(d["coef"])
            bias, sm, ss = finite([d["bias"], d["score"]["mean"], d["score"]["std"]])
            if len(coef) != len(FEATURES) or ss <= 0:
                return None
            return cls(coef, bias, str(d.get("scoring", "mean")), sm, ss,
                       {str(k): st(v) for k, v in (d.get("devices") or {}).items()},
                       st(d["pooled"]), dict(d.get("fit") or {}))
        except (KeyError, TypeError, ValueError, AttributeError):
            return None


def fit(rows, scoring: str = "mean", device_stats: dict | None = None, l2: float = L2, cols=None) -> Model:
    """Fit on rows [(device, (level, decay, flat), score, y)] (y 1 = the wearer). Acoustic
    features are z-scored with `device_stats` ({device: stats()}, default: from these
    rows); Newton's method on the L2-penalized log loss (the bias isn't penalized). `cols`:
    indices into FEATURES to use (the rest get coefficient 0; for comparisons)."""
    devs = sorted({r[0] for r in rows})
    if device_stats is None:
        device_stats = {d: stats([r[1] for r in rows if r[0] == d]) for d in devs}
    pooled = stats([r[1] for r in rows])
    scores = np.array([r[2] for r in rows], dtype=np.float64)
    sm, ss = float(scores.mean()), float(max(scores.std(), 1e-6))
    m = Model([0.0] * len(FEATURES), 0.0, scoring, sm, ss, device_stats, pooled)
    X = np.array([[*((np.asarray(r[1]) - m.norm(r[0])["mean"]) / np.asarray(m.norm(r[0])["std"])),
                   (r[2] - sm) / ss, 1.0] for r in rows], dtype=np.float64)
    if cols is not None:
        X[:, [j for j in range(len(FEATURES)) if j not in cols]] = 0.0
    y = np.array([r[3] for r in rows], dtype=np.float64)
    w = np.zeros(X.shape[1])
    pen = np.full(X.shape[1], l2)
    pen[-1] = 0.0
    for _ in range(50):
        p = sigmoid(X @ w)
        g = X.T @ (p - y) + pen * w
        H = (X * (p * (1 - p))[:, None]).T @ X + np.diag(pen) + 1e-9 * np.eye(len(w))
        step = np.linalg.solve(H, g)
        w -= step
        if np.abs(step).max() < 1e-8:
            break
    m.coef, m.bias = [float(v) for v in w[:-1]], float(w[-1])
    return m


@dataclass
class Decision:
    """What the wearer step does to one voice."""
    p: float | None = None
    name: bool = False      # name it the wearer
    unname: bool = False    # take the wearer's name away


def decide(p: float | None, named_wearer: bool, voice_score: float | None, strict: float,
           wearer_p: float) -> Decision:
    """p >= wearer_p: the wearer. p <= 1 - wearer_p: not the wearer by voiceprint unless the
    voice's own score for him reaches `strict`."""
    d = Decision(p)
    if p is None:
        return d
    if p >= wearer_p and not named_wearer:
        d.name = True
    elif p <= 1 - wearer_p and named_wearer and not (voice_score is not None and voice_score >= strict):
        d.unname = True
    return d
