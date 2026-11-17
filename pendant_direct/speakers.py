"""Local speaker recognition for one conversation clip (CPU only).

1. Group whisper segments into spans worth embedding (very short ones merge with
   their neighbour; spans under MIN_SPAN_S are not embedded and inherit a label).
2. Embed each span with SpeechBrain ECAPA (speechbrain/spkrec-ecapa-voxceleb).
3. Agglomerative clustering (average linkage, cosine) within the conversation.
4. Name clusters against enrolled voiceprints (`voiceprints/<name>.npz`: per-cluster
   samples plus legacy history, see below); the rest become Speaker A, B, ...

Enrollment is consent-gated: only names in config `speakers.allowed`.
The embedder is injectable (tests use a fake): any callable
`embedder(clip: Path, spans: [(start_s, end_s)]) -> np.ndarray (n, dim)`.
"""
from __future__ import annotations

import json
import re
import string
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

import numpy as np

from . import closeness

SAMPLE_RATE = 16_000
MIN_SPAN_S = 1.0      # don't embed less audio than this
TARGET_SPAN_S = 2.0   # keep merging short neighbours up to this
MERGE_GAP_S = 1.0     # only merge segments this close together
MIN_CLUSTER_S = 20.0   # less speech than this (or 3% of the talk) folds into a bigger voice
MAX_CLUSTER_N = 300   # cluster at most this many spans; assign the rest by centroid
WINDOW_S = 1.5        # word-level turns: voice embedding window...
HOP_S = 0.75          # ...and hop
MAX_WINDOWS = 2400    # a clip needing more windows (30 min of speech) uses segment-level turns
MIN_WINDOW_VOICED_S = 0.4   # a window with less word time than this isn't embedded
MIN_RUN_S = 1.0       # a voice change shorter than this is not a turn
EMBED_BATCH = 32
SNIPPET_MAX_S = 12.0
SNIPPET_MIN_S = 3.0
MIN_VOICED = 0.4     # share of the snippet Silero calls speech; short 3 s snippets with pauses of real speech measured 0.4, noise 0.0
MIN_WORDS = 4         # ...and its transcript at least this many words
MODEL_SOURCE = "speechbrain/spkrec-ecapa-voxceleb"
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z .'-]{0,39}$")


class ConsentError(Exception):
    """Enrollment refused: the name isn't in speakers.allowed."""


# ---------------------------------------------------------------------------
# audio + embedding model

def load_audio(path: Path, sr: int = SAMPLE_RATE) -> np.ndarray:
    """Decode a clip to float32 mono at sr Hz with faster-whisper's decoder, the
    same one transcription uses (it tolerates our Ogg tag header; plain
    av.open with strict metadata parsing rejects it)."""
    from faster_whisper.audio import decode_audio
    return np.asarray(decode_audio(str(path), sampling_rate=sr), dtype=np.float32)


class EcapaEmbedder:
    """SpeechBrain ECAPA-TDNN speaker embeddings (192-d), loaded lazily on CPU."""

    def __init__(self, savedir: Path):
        self.savedir = Path(savedir)
        self._model = None

    def model(self):
        if self._model is None:
            import torch
            from speechbrain.inference.speaker import EncoderClassifier
            torch.set_num_threads(max(1, (__import__("os").cpu_count() or 4) // 2))
            self._model = EncoderClassifier.from_hparams(
                source=MODEL_SOURCE, savedir=str(self.savedir), run_opts={"device": "cpu"})
        return self._model

    def embed_wavs(self, wavs: list[np.ndarray]) -> np.ndarray:
        import torch
        m = self.model()
        out: list = [None] * len(wavs)
        by_len: dict[int, list[int]] = {}
        for i, w in enumerate(wavs):
            by_len.setdefault(len(w), []).append(i)
        with torch.no_grad():
            # same-length waveforms go through ECAPA as one batch (no padding needed)
            for idx in by_len.values():
                for j in range(0, len(idx), EMBED_BATCH):
                    part = idx[j:j + EMBED_BATCH]
                    x = torch.from_numpy(np.stack([np.ascontiguousarray(wavs[i]) for i in part]))
                    e = m.encode_batch(x).reshape(len(part), -1).cpu().numpy()
                    for i, v in zip(part, e):
                        out[i] = v
        return np.stack(out).astype(np.float32)

    def __call__(self, clip: Path, spans) -> np.ndarray:
        return self.embed_audio(load_audio(clip), spans)

    def embed_audio(self, audio: np.ndarray, spans) -> np.ndarray:
        """Embeddings of [(start_s, end_s)] cut from audio (16 kHz)."""
        wavs = []
        for s, e in spans:
            w = audio[int(s * SAMPLE_RATE):int(e * SAMPLE_RATE)]
            wavs.append(w if len(w) >= SAMPLE_RATE // 2 else np.pad(w, (0, SAMPLE_RATE // 2 - len(w))))
        return self.embed_wavs(wavs)


_embedder = None


def get_embedder(data_dir: Path):
    global _embedder
    if _embedder is None:
        _embedder = EcapaEmbedder(Path(data_dir) / "models" / "spkrec-ecapa-voxceleb")
    return _embedder


# ---------------------------------------------------------------------------
# audio cleanup for the voice model only (`speakers.denoise`): Whisper never sees it

DN_FRAME = 512        # 32 ms STFT frames...
DN_HOP = 128          # ...every 8 ms
DN_BLOCK_S = 30.0     # the noise floor is re-estimated every this many seconds (TV, kitchen come and go)
DN_PERCENTILE = 15    # a frequency's noise floor: this percentile of its magnitude within the block
DN_ALPHA = 1.5        # gain = 1 - alpha * floor / magnitude...
DN_FLOOR = 0.1        # ...never below this (no musical-noise holes)


def denoise(audio: np.ndarray, spans=None, sr: int = SAMPLE_RATE) -> np.ndarray:
    """Spectral gating (numpy only): a new array where each frequency's noise floor, estimated
    per DN_BLOCK_S block, is gated out with a soft gain smoothed over time and frequency. Only
    frames overlapping `spans` [(start_s, end_s)] are processed (the rest is zero); None: all.
    The input is never modified."""
    x = np.asarray(audio, dtype=np.float32)
    n = len(x)
    out = np.zeros(n, dtype=np.float32)
    if n < DN_FRAME:
        return x.copy()
    win = np.hanning(DN_FRAME + 1)[:-1].astype(np.float32)   # analysis and synthesis; OLA divides by sum(win^2)
    n_frames = 1 + (n - DN_FRAME) // DN_HOP
    want = np.ones(n_frames, dtype=bool)
    if spans is not None:
        want[:] = False
        starts = np.arange(n_frames) * DN_HOP
        for s, e in spans:
            a, b = int(s * sr) - DN_FRAME, int(e * sr)
            want[(starts >= a) & (starts < b)] = True
    norm = np.zeros(n, dtype=np.float32)
    per_block = max(1, int(DN_BLOCK_S * sr) // DN_HOP)
    for f0 in range(0, n_frames, per_block):
        f1 = min(n_frames, f0 + per_block)
        if not want[f0:f1].any():
            continue
        idx = np.arange(f0, f1)[:, None] * DN_HOP + np.arange(DN_FRAME)[None, :]
        spec = np.fft.rfft(x[idx] * win, axis=1)
        mag = np.abs(spec)
        floor = np.percentile(mag, DN_PERCENTILE, axis=0)
        gain = np.clip(1.0 - DN_ALPHA * floor[None, :] / np.maximum(mag, 1e-9), DN_FLOOR, 1.0)
        gain = (np.pad(gain, ((1, 1), (0, 0)), mode="edge")[:-2] + gain
                + np.pad(gain, ((1, 1), (0, 0)), mode="edge")[2:]) / 3          # 3 frames
        gain = (np.pad(gain, ((0, 0), (1, 1)), mode="edge")[:, :-2] + gain
                + np.pad(gain, ((0, 0), (1, 1)), mode="edge")[:, 2:]) / 3       # 3 bins
        keep = want[f0:f1]
        frames = np.fft.irfft(spec[keep] * gain[keep], n=DN_FRAME, axis=1).astype(np.float32) * win
        rows = idx[keep]
        np.add.at(out, rows.ravel(), frames.ravel())
        np.add.at(norm, rows.ravel(), np.broadcast_to(win * win, frames.shape).ravel())
    ok = norm > 1e-3
    out[ok] /= norm[ok]
    return out


class DenoisedEmbedder:
    """An embedder (EcapaEmbedder, or anything with embed_audio(audio, spans)) fed a denoised
    copy of the clip's audio. The clip file and the decoded audio are left as they are, so the
    transcription never sees the cleanup. `cpu_s` adds up the time spent denoising."""

    def __init__(self, inner, audio_loader=None):
        self.inner = inner
        self.audio_loader = audio_loader
        self.cpu_s = 0.0

    def __call__(self, clip: Path, spans) -> np.ndarray:
        import time
        audio = (self.audio_loader or load_audio)(clip)
        t0 = time.process_time()
        clean = denoise(audio, spans)
        self.cpu_s += time.process_time() - t0
        return self.inner.embed_audio(clean, spans)


def speech_timestamps(audio: np.ndarray, sr: int = SAMPLE_RATE) -> list[tuple[float, float]]:
    """Speech spans in seconds per Silero VAD (bundled with faster-whisper, CPU)."""
    from faster_whisper.vad import get_speech_timestamps
    ts = get_speech_timestamps(audio, sampling_rate=sr, min_silence_duration_ms=300, speech_pad_ms=30)
    return [(t["start"] / sr, t["end"] / sr) for t in ts]


def best_voice_window(audio: np.ndarray, start_s: float, end_s: float, sr: int = SAMPLE_RATE,
                      vad=None) -> tuple[float, float, float]:
    """The most speech-dense SNIPPET_MIN_S..SNIPPET_MAX_S window inside
    [start_s, end_s] of audio: (window_start, window_end, voiced_fraction).
    Among windows at or above MIN_VOICED the one with the most speech wins,
    else the densest. A span shorter than SNIPPET_MIN_S is scored whole.
    `vad(audio_slice, sr)` returns [(start_s, end_s)] relative to the slice
    (default: Silero)."""
    total = len(audio) / sr
    start_s = max(0.0, min(start_s, total))
    end_s = max(start_s, min(end_s, total))
    span = end_s - start_s
    if span <= 0:
        return start_s, end_s, 0.0
    chunk = np.asarray(audio[int(start_s * sr):int(end_s * sr)], dtype=np.float32)
    spans = (vad or speech_timestamps)(chunk, sr)
    step = 0.1
    n = max(1, int(round(span / step)))
    mask = np.zeros(n, dtype=np.float64)
    for a, b in spans:
        mask[max(0, int(a / step)):max(0, min(n, int(np.ceil(b / step))))] = 1.0
    cum = np.concatenate([[0.0], np.cumsum(mask)])
    if span <= SNIPPET_MIN_S:
        lengths = [n]
    else:
        top = min(SNIPPET_MAX_S, span)
        lengths = sorted({int(round(L / step)) for L in np.arange(SNIPPET_MIN_S, top + 1e-9, 1.0)} |
                         {int(round(top / step))})
    best = None
    for L in lengths:
        for i in range(0, n - L + 1, 5):
            voiced = (cum[i + L] - cum[i]) / L
            key = (voiced >= MIN_VOICED, (voiced * L) if voiced >= MIN_VOICED else voiced)
            if best is None or key > best[0]:
                best = (key, i, L, voiced)
    _k, i, L, voiced = best
    return start_s + i * step, start_s + (i + L) * step, float(voiced)


# ---------------------------------------------------------------------------
# pure helpers

def normalize(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    n = np.linalg.norm(x, axis=-1, keepdims=True)
    return x / np.maximum(n, 1e-9)


def group_segments(segs, min_s: float = MIN_SPAN_S, target_s: float = TARGET_SPAN_S,
                   gap_s: float = MERGE_GAP_S) -> list[tuple[float, float, list[int]]]:
    """[(start, end, [segment indices])] to embed. Consecutive segments merge
    while the group is shorter than target_s and the gap is under gap_s; groups
    still shorter than min_s are dropped (their segments inherit a label)."""
    groups: list[list] = []
    for i, (s, e, _t) in enumerate(segs):
        if groups and groups[-1][1] - groups[-1][0] < target_s and s - groups[-1][1] < gap_s:
            groups[-1][1] = max(groups[-1][1], e)
            groups[-1][2].append(i)
        else:
            groups.append([s, e, [i]])
    return [(s, e, idx) for s, e, idx in groups if e - s >= min_s]


def agglomerative(embs: np.ndarray, threshold: float) -> np.ndarray:
    """Average-linkage clustering on cosine similarity; merges while the best
    pair's mean similarity >= threshold. Returns a cluster label per row."""
    n = len(embs)
    if n == 0:
        return np.zeros(0, dtype=int)
    x = normalize(embs)
    sim = (x @ x.T).astype(np.float64)
    np.fill_diagonal(sim, -np.inf)
    size = np.ones(n)
    alive = np.ones(n, dtype=bool)
    labels = np.arange(n)
    while alive.sum() > 1:
        flat = np.argmax(sim)
        a, b = divmod(int(flat), n)
        if sim[a, b] < threshold:
            break
        new = (size[a] * sim[a] + size[b] * sim[b]) / (size[a] + size[b])
        sim[a, :] = new
        sim[:, a] = new
        sim[a, a] = -np.inf
        sim[b, :] = -np.inf
        sim[:, b] = -np.inf
        size[a] += size[b]
        alive[b] = False
        labels[labels == b] = a
    _, labels = np.unique(labels, return_inverse=True)
    return labels


def cluster(embs: np.ndarray, durations: np.ndarray, threshold: float,
            min_cluster_s: float = MIN_CLUSTER_S, max_n: int = MAX_CLUSTER_N) -> np.ndarray:
    """Cluster spans; the longest max_n are clustered and the rest join the
    nearest centroid. Clusters with < min_cluster_s of speech fold into the
    nearest cluster that has enough (if there is one)."""
    n = len(embs)
    if n == 0:
        return np.zeros(0, dtype=int)
    x = normalize(embs)
    core = np.argsort(-durations)[:max_n]
    labels = np.full(n, -1)
    labels[core] = agglomerative(x[core], threshold)
    rest = labels < 0
    if rest.any():
        cents = normalize(np.stack([x[labels == k].mean(0) for k in range(labels.max() + 1)]))
        labels[rest] = np.argmax(x[rest] @ cents.T, axis=1)
    speech = np.bincount(labels, weights=durations)
    floor = max(min_cluster_s, 0.03 * float(durations.sum()))
    big = np.flatnonzero(speech >= floor)
    if len(big):
        cents = normalize(np.stack([x[labels == k].mean(0) for k in range(len(speech))]))
        for k in np.flatnonzero(speech < floor):
            labels[labels == k] = big[np.argmax(cents[big] @ cents[k])]
    else:
        labels = _fold_fragments(x, labels, durations, min(floor, NO_BIG_SHARE * float(durations.sum())))
    _, labels = np.unique(labels, return_inverse=True)
    return labels


NO_BIG_SHARE = 0.15   # with no voice at the floor, fold until each has this share of the talk (or the floor)


def _fold_fragments(x: np.ndarray, labels: np.ndarray, durations: np.ndarray, floor: float) -> np.ndarray:
    """When no voice reaches the floor (noisy audio scatters every voice into fragments of a
    few seconds), there is no big voice to fold into: merge the voice with the least speech
    into its nearest voice (centroid cosine), one at a time, until every voice has `floor`
    of speech or one voice is left."""
    labels = labels.copy()
    while True:
        ks = np.unique(labels)
        if len(ks) < 2:
            return labels
        speech = np.array([durations[labels == k].sum() for k in ks])
        s = int(np.argmin(speech))
        if speech[s] >= floor:
            return labels
        cents = normalize(np.stack([x[labels == k].mean(0) for k in ks]))
        sims = cents @ cents[s]
        sims[s] = -np.inf
        labels[labels == ks[s]] = ks[int(np.argmax(sims))]


def _clear_winner(scores, j: int, margin: float) -> bool:
    """Whether person j's score beats every other person's finite score by at least `margin`."""
    others = [float(v) for k, v in enumerate(scores) if k != j and np.isfinite(v)]
    return not others or float(scores[j]) - max(others) >= margin


def naming_settings(sp: dict) -> dict:
    """The `match` keywords set in the `speakers` config besides threshold and margin."""
    from .config import DEFAULTS
    d = DEFAULTS["speakers"]
    v = sp.get("split_voice_cosine", d["split_voice_cosine"])
    from .config import wearer
    return {"split_cosine": None if v is None else float(v),
            "min_samples": int(sp.get("min_samples", d["min_samples"]) or 0), "wearer": wearer(sp)}


def sample_count(name: str, voiceprints: dict, calibration=None) -> int:
    """How many samples back name's voiceprint (the legacy part counts as its own count)."""
    if name in voiceprints:
        return int(voiceprints[name][1])
    samples = getattr(calibration, "samples", None) or {}
    return len(samples[name][1]) if name in samples else 0


def thin_names(names, voiceprints: dict, calibration=None, min_samples: int = 0, wearer: str | None = None) -> set:
    """The names in `names` backed by fewer than min_samples samples (speakers.min_samples), never
    the wearer's: nobody is named after them automatically."""
    w = (wearer or "").lower()
    return {n for n in names if n and n.lower() != w and sample_count(n, voiceprints, calibration) < min_samples}


def unname_thin(names: list, thin: set) -> list:
    """names with every thin name left unnamed (None); nothing else changes."""
    return [None if n in thin else n for n in names]


def _shares_name(cn: np.ndarray, i: int, holders: list[int], split_cosine: float | None) -> bool:
    """Whether voice i may take a name `holders` already have: every holder's centroid is within
    split_cosine of its own (one person's voice split in two), never when split_cosine is None."""
    return split_cosine is not None and all(float(cn[i] @ cn[h]) >= split_cosine for h in holders)


def match(centroids: np.ndarray, voiceprints: dict[str, tuple[np.ndarray, int]],
          threshold: float, strangers: np.ndarray | None = None,
          calibration=None, exclude=None, prior=None, margin: float = 0.0,
          extra: dict[str, float] | None = None, split_cosine: float | None = None,
          min_samples: int = 0, wearer: str | None = None) -> list[str | None]:
    """Name each centroid after the closest voiceprint at or above threshold;
    each name is used at most once per conversation (best pairs first), unless
    `split_cosine` (speakers.split_voice_cosine) is set: then a voice that clears the
    person on its own may share the name with the voices already holding it when its
    centroid is within that cosine of each of theirs (one person split in two). Naming then
    leaves unnamed every voice won by a person backed by fewer than `min_samples` samples
    (speakers.min_samples; never the `wearer`): the voice is never handed to the runner-up. A
    centroid closer to a stored stranger embedding (a voice the wearer called "a
    random person") than to a voiceprint is not named after that voiceprint.
    With `calibration` (calibration.Calibration) the centroids are scored against
    each person's samples with s-norm instead, `exclude` being this conversation's
    key; `threshold` then only applies on the fallback scale. `prior` ({name: float},
    closeness.py) reorders pairs that each clear their own threshold (by margin above it);
    it never names a voice that wouldn't be named without it. `margin` (cosine units,
    speakers.match_margin): a voice is only named after the person who beats every other
    person's raw score for it by at least this much, so a weak or ambiguous match stays
    unnamed; 0 turns it off. `extra` ({lowercased name: cosine}, context.extra_bars): a person
    unexpected in this conversation's context needs that much more raw score to be named; a
    barred winner leaves the voice unnamed and nobody else takes its place."""
    ex = {k.lower(): float(v) for k, v in (extra or {}).items() if v}
    out: list[str | None] = [None] * len(centroids)
    if calibration is not None and len(centroids) and calibration.names:
        res = calibration.scores(centroids, exclude=exclude)
        cn = normalize(centroids)
        near = np.full(len(centroids), -np.inf)
        if strangers is not None and len(strangers):
            near = (cn @ normalize(strangers).T).max(axis=1)
        pv = closeness.vector(calibration.names, prior)
        pairs = sorted(((closeness.pair_key(res.match[i, j], res.threshold[i, j], None if pv is None else pv[j]), i, j)
                        for i in range(len(centroids))
                        for j in range(len(calibration.names)) if np.isfinite(res.match[i, j])), reverse=True)
        used: dict[int, list[int]] = {}
        for _key, i, j in pairs:
            if res.match[i, j] < res.threshold[i, j] or res.raw[i, j] <= near[i]:
                continue
            if margin > 0 and not _clear_winner(res.raw[i], j, margin):
                continue
            if out[i] is None and (j not in used or _shares_name(cn, i, used[j], split_cosine)):
                out[i] = calibration.names[j]
                used.setdefault(j, []).append(i)
        if ex:
            col = {n: j for j, n in enumerate(calibration.names)}
            for i, n in enumerate(out):
                bar = ex.get(str(n).lower(), 0.0) if n else 0.0
                if bar:
                    floor = calibration.raw_threshold if not res.calibrated[i, col[n]] else MIN_RAW_FOR_BARS
                    if res.raw[i, col[n]] < floor + bar:
                        out[i] = None
        return unname_thin(out, thin_names(out, voiceprints, calibration, min_samples, wearer))
    if not len(centroids) or not voiceprints:
        return out
    names = list(voiceprints)
    vp = normalize(np.stack([voiceprints[n][0] for n in names]))
    cn = normalize(centroids)
    sims = cn @ vp.T
    near_stranger = np.full(len(centroids), -np.inf)
    if strangers is not None and len(strangers):
        near_stranger = (cn @ normalize(strangers).T).max(axis=1)
    pv = closeness.vector(names, prior)
    pairs = sorted(((closeness.pair_key(sims[i, j], threshold, None if pv is None else pv[j]), i, j)
                    for i in range(len(centroids)) for j in range(len(names))), reverse=True)
    used: dict[int, list[int]] = {}
    for _key, i, j in pairs:
        s = sims[i, j]
        if s < threshold:
            continue
        if s <= near_stranger[i]:
            continue
        if margin > 0 and not _clear_winner(sims[i], j, margin):
            continue
        if out[i] is None and (j not in used or _shares_name(cn, i, used[j], split_cosine)):
            out[i] = names[j]
            used.setdefault(j, []).append(i)
    if ex:
        for i, n in enumerate(out):
            bar = ex.get(str(n).lower(), 0.0) if n else 0.0
            if bar and sims[i, names.index(n)] < threshold + bar:
                out[i] = None
    return unname_thin(out, thin_names(out, voiceprints, None, min_samples, wearer))


MIN_RAW_FOR_BARS = 0.4   # calibrated pairs: the raw floor a context bar is added to (calibration.MIN_RAW)


class ScoreMatrix(NamedTuple):
    """Scores of n voices against P people: `match` is what `threshold` applies to (the
    s-norm score when calibrated, else the raw score), `raw` the cosine (mean mode) or
    top-k cosine, `calibrated` which scale each entry is on; -inf where a person has no
    usable sample."""
    names: list
    match: np.ndarray
    raw: np.ndarray
    threshold: np.ndarray
    calibrated: np.ndarray


def score_matrix(x: np.ndarray, voiceprints: dict, threshold: float, calibration=None,
                 exclude=None, self_ids=None) -> ScoreMatrix:
    """The scorer for `speakers.scoring`, shared by Voice Scoring (voices.score) and turn
    rescoring: with `calibration` (calibration.Calibration, "topk" or "calibrated") each
    person's samples, otherwise the cosine to each voiceprint's mean ({name: (mean,
    count)}) against `threshold`."""
    x = normalize(np.asarray(x, dtype=np.float32).reshape(len(x), -1))
    n = len(x)
    if calibration is not None and calibration.names:
        res = calibration.scores(x, exclude=exclude, self_ids=self_ids)
        return ScoreMatrix(list(calibration.names), res.match, res.raw, res.threshold, res.calibrated)
    names = list(voiceprints)
    if names:
        sims = x @ normalize(np.stack([voiceprints[k][0] for k in names])).T
    else:
        sims = np.zeros((n, 0), np.float32)
    return ScoreMatrix(names, sims, sims, np.full(sims.shape, float(threshold)), np.zeros(sims.shape, dtype=bool))


# ---------------------------------------------------------------------------
# voiceprints
#
# One file per person, `voiceprints/<name>.npz`:
#   legacy_sum, legacy  - the opaque history from before per-cluster tracking
#                         (old `<name>.npy` mean x `<name>.json` count), and
#                         samples enrolled without a cluster id
#   ids, embs           - one unit embedding per contributing speaker cluster
# The voiceprint is (legacy_sum + embs.sum()) / (legacy + len(ids)). Adding a
# cluster that is already a member, or removing one that isn't, changes
# nothing, so an answer can be retried safely; legacy history is never removed
# by id. Every write holds `voiceprints/.lock` (feed and processor alike) and
# lands through a unique temp file + os.replace.

def voiceprint_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "voiceprints"


@dataclass
class Voiceprint:
    legacy_sum: np.ndarray
    legacy: int
    ids: list[int]
    embs: np.ndarray            # (len(ids), dim)

    @property
    def count(self) -> int:
        return self.legacy + len(self.ids)

    def mean(self) -> np.ndarray:
        total = self.legacy_sum + (self.embs.sum(0) if len(self.ids) else 0)
        return (total / max(self.count, 1)).astype(np.float32)


def _read(vdir: Path, name: str) -> Voiceprint | None:
    """name's voiceprint: the .npz, else the old mean/count pair (read as legacy)."""
    vdir = Path(vdir)
    z = vdir / f"{name}.npz"
    try:
        if z.exists():
            with np.load(z) as d:
                legacy_sum = d["legacy_sum"].astype(np.float32).reshape(-1)
                ids = [int(i) for i in d["ids"]]
                embs = d["embs"].astype(np.float32).reshape(len(ids), len(legacy_sum))
                return Voiceprint(legacy_sum, int(d["legacy"]), ids, embs)
        old = vdir / f"{name}.npy"
        if old.exists():
            vec = np.load(old).astype(np.float32).reshape(-1)
            meta = vdir / f"{name}.json"
            count = int(json.loads(meta.read_text()).get("count", 1)) if meta.exists() else 1
            return Voiceprint(vec * count, count, [], np.zeros((0, len(vec)), np.float32))
    except (OSError, ValueError, KeyError):
        return None
    return None


def _write(vdir: Path, name: str, vp: Voiceprint | None) -> None:
    """Replace name's voiceprint (None or empty deletes it). Caller holds the lock."""
    import os
    import tempfile
    vdir = Path(vdir)
    z = vdir / f"{name}.npz"
    if vp is None or vp.count <= 0:
        z.unlink(missing_ok=True)
    else:
        fd, tmp = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=vdir)
        try:
            with os.fdopen(fd, "wb") as f:
                np.savez(f, legacy_sum=vp.legacy_sum.astype(np.float32), legacy=np.int64(vp.legacy),
                         ids=np.asarray(vp.ids, dtype=np.int64), embs=vp.embs.astype(np.float32))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, z)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    # the old pair is folded into the .npz (or gone with it)
    (vdir / f"{name}.npy").unlink(missing_ok=True)
    (vdir / f"{name}.json").unlink(missing_ok=True)


@contextmanager
def voiceprint_lock(vdir: Path):
    """Serializes every voiceprint write (feed answers and processor enrollments)."""
    import fcntl
    vdir = Path(vdir)
    vdir.mkdir(parents=True, exist_ok=True)
    with open(vdir / ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _names(vdir: Path) -> list[str]:
    vdir = Path(vdir)
    return sorted({f.stem for f in vdir.glob("*.npz")} | {f.stem for f in vdir.glob("*.npy")}
                  - {""}) if vdir.exists() else []


def load_voiceprints(vdir: Path) -> dict[str, tuple[np.ndarray, int]]:
    """{name: (mean embedding, sample count)}."""
    out = {}
    for name in _names(vdir):
        if name.startswith("."):
            continue
        vp = _read(vdir, name)
        if vp is not None and vp.count > 0:
            out[name] = (vp.mean(), vp.count)
    return out


def load_samples(vdir: Path) -> dict[str, tuple[np.ndarray, list]]:
    """{name: (unit sample embeddings, cluster ids)}: every contributed sample,
    plus the legacy history as one sample (its mean, id None) until replaced."""
    out = {}
    for name in _names(vdir):
        if name.startswith("."):
            continue
        vp = _read(vdir, name)
        if vp is None or vp.count <= 0:
            continue
        rows, ids = [], []
        if vp.legacy > 0 and float(np.abs(vp.legacy_sum).sum()) > 0:
            rows.append(vp.legacy_sum / vp.legacy)
            ids.append(None)
        for e, cid in zip(vp.embs, vp.ids):
            rows.append(e)
            ids.append(cid)
        if rows:
            out[name] = (normalize(np.stack(rows)), ids)
    return out


def members(vdir: Path, name: str) -> list[int]:
    vp = _read(vdir, name)
    return list(vp.ids) if vp else []


def migrate_voiceprints(vdir: Path) -> int:
    """Fold old `<name>.npy` + `<name>.json` pairs into `.npz` files as legacy
    history. Returns how many were migrated."""
    n = 0
    if not Path(vdir).exists():
        return 0
    with voiceprint_lock(vdir):
        for name in _names(vdir):
            if (Path(vdir) / f"{name}.npy").exists() and not (Path(vdir) / f"{name}.npz").exists():
                _write(vdir, name, _read(vdir, name))
                n += 1
    return n


def sample_key(sample_id: str) -> int:
    """A stable negative integer for a non-cluster sample (e.g. a single line,
    `para:<device>:<start_ms>:<index>`), so it can live in a voiceprint's
    cluster-id list without ever colliding with a (positive) cluster id."""
    import hashlib
    return -(int.from_bytes(hashlib.sha256(sample_id.encode()).digest()[:6], "big") + 1)


def allowed_name(name: str, allowed: list[str]) -> str | None:
    """The allowed spelling of name (case-insensitive), or None."""
    name = (name or "").strip()
    for a in allowed:
        if a.lower() == name.lower() and NAME_RE.match(a):
            return a
    return None


def add(vdir: Path, name: str, cluster_id: int | None, embedding: np.ndarray) -> int:
    """Fold a cluster's unit embedding into name's voiceprint. A cluster that is
    already a member is not added again; cluster_id None adds to the legacy
    history (not removable). Returns the count. No consent check: use enroll."""
    e = normalize(embedding).reshape(-1)
    with voiceprint_lock(vdir):
        vp = _read(vdir, name) or Voiceprint(np.zeros_like(e), 0, [], np.zeros((0, len(e)), np.float32))
        if cluster_id is None:
            vp.legacy_sum = vp.legacy_sum + e
            vp.legacy += 1
        elif int(cluster_id) in vp.ids:
            return vp.count
        else:
            vp.ids.append(int(cluster_id))
            vp.embs = np.vstack([vp.embs, e[None, :]])
        _write(vdir, name, vp)
        return vp.count


def remove(vdir: Path, name: str, cluster_id: int) -> int:
    """Take a cluster's sample out of name's voiceprint (no-op when it isn't a
    member, including samples inside the legacy history). The last sample
    removes the voiceprint. Returns the count (0 when gone or never there)."""
    with voiceprint_lock(vdir):
        vp = _read(vdir, name)
        if vp is None:
            return 0
        if int(cluster_id) not in vp.ids:
            return vp.count
        keep = [i for i, c in enumerate(vp.ids) if c != int(cluster_id)]
        vp.ids = [vp.ids[i] for i in keep]
        vp.embs = vp.embs[keep]
        _write(vdir, name, vp)
        return vp.count


def replace_samples(vdir: Path, name: str, drop_ids, items: list[tuple[int, np.ndarray]]) -> int:
    """In one locked write: take every sample keyed by one of `drop_ids` out of name's
    voiceprint, then add each (id, embedding) of `items`. Re-running with the same
    drop_ids leaves exactly the latest items, however a previous run ended. No consent
    check: use after enroll's. Returns the voiceprint's count."""
    drop = {int(i) for i in drop_ids}
    with voiceprint_lock(vdir):
        dim = len(items[0][1]) if items else 0
        vp = _read(vdir, name)
        if vp is None:
            vp = Voiceprint(np.zeros(dim, np.float32), 0, [], np.zeros((0, dim), np.float32))
        keep = [i for i, c in enumerate(vp.ids) if c not in drop]
        ids, embs = [vp.ids[i] for i in keep], vp.embs[keep]
        for cid, e in items:
            if int(cid) not in ids:
                ids.append(int(cid))
                embs = np.vstack([embs, normalize(e).reshape(1, -1)])
        vp.ids, vp.embs = ids, embs
        _write(vdir, name, vp)
        return vp.count


def take(vdir: Path, ids, dry_run: bool = False) -> list[dict]:
    """Take every sample keyed by one of `ids` out of every voiceprint, exactly
    as stored. Returns [{"name", "index", "id", "emb"}] for put() to restore
    (index is the sample's position in that voiceprint before the take).
    dry_run: only say what would be taken."""
    want = {int(i) for i in ids}
    taken: list[dict] = []
    if not want or not Path(vdir).exists():
        return taken
    with voiceprint_lock(vdir):
        for name in _names(vdir):
            if name.startswith("."):
                continue
            vp = _read(vdir, name)
            if vp is None or not want.intersection(vp.ids):
                continue
            keep = [i for i, c in enumerate(vp.ids) if c not in want]
            taken += [{"name": name, "index": i, "id": c, "emb": [float(x) for x in vp.embs[i]]}
                      for i, c in enumerate(vp.ids) if c in want]
            if dry_run:
                continue
            vp.ids = [vp.ids[i] for i in keep]
            vp.embs = vp.embs[keep]
            _write(vdir, name, vp)
    return taken


def put(vdir: Path, taken: list[dict]) -> int:
    """Put back samples from take(), each at its old position (so a take then a
    put leaves the voiceprint as it was). A sample whose id is already in that
    voiceprint is left alone, so putting twice changes nothing. Returns how
    many were added."""
    added = 0
    by_name: dict[str, list[dict]] = {}
    for t in taken:
        by_name.setdefault(t["name"], []).append(t)
    for name, items in by_name.items():
        with voiceprint_lock(vdir):
            dim = len(items[0]["emb"])
            vp = _read(vdir, name) or Voiceprint(np.zeros(dim, np.float32), 0, [], np.zeros((0, dim), np.float32))
            ids, rows = list(vp.ids), list(vp.embs)
            n = 0
            for t in sorted(items, key=lambda t: t["index"]):
                if int(t["id"]) in ids:
                    continue
                at = min(int(t["index"]), len(ids))
                ids.insert(at, int(t["id"]))
                rows.insert(at, np.asarray(t["emb"], dtype=np.float32))
                n += 1
            if n:
                vp.ids = ids
                vp.embs = np.stack(rows).astype(np.float32)
                _write(vdir, name, vp)
                added += n
    return added


def enroll(vdir: Path, name: str, embedding: np.ndarray, allowed: list[str],
           cluster_id: int | None = None) -> int:
    """Consent-gated add: raises ConsentError unless name is allowed. Returns the count."""
    canonical = allowed_name(name, allowed)
    if canonical is None:
        raise ConsentError(name)
    return add(vdir, canonical, cluster_id, embedding)


# ---------------------------------------------------------------------------
# one conversation

@dataclass
class Cluster:
    tag: str                    # "Speaker A" or the matched name
    name: str | None            # matched voiceprint, else None
    embedding: np.ndarray       # centroid (unit)
    speech_s: float
    start_s: float              # representative snippet within the clip
    end_s: float
    excerpt: str
    mixed: bool = False         # turn rescoring took most of its speech away: two people in one voice
    wearer_named: bool = False  # The wearer signal (not a voiceprint) gave it the wearer's name
    index: int = -1             # its cluster number inside diarize_words
    match_score: float | None = None   # its score for the person it was named after (process.attach_match_scores)


@dataclass
class Diarization:
    labels: list[str | None]    # per whisper segment
    clusters: list[Cluster] = field(default_factory=list)
    show: bool = False          # render labels: a voiceprint matched, or 2+ voices took turns
    turns: list[Turn] | None = None   # word-level pieces (diarize_words); None = per-segment labels
    rescore: dict | None = None       # what turn rescoring did (or its error); None when it didn't run
    before: list[Turn] | None = None  # the cluster-level turns, when diarize_words was asked to keep them
    boundaries: dict | None = None    # what the turn-boundary passes did (Boundaries), per pass


def diarize(clip: Path, segs, embedder, voiceprints: dict, *, cluster_threshold: float,
            match_threshold: float, strangers=None, calibration=None,
            exclude=None, prior=None, match_margin: float = 0.0, extra=None,
            split_cosine: float | None = None, min_samples: int = 0, wearer: str | None = None) -> Diarization:
    groups = group_segments(segs)
    if not groups:
        return Diarization(labels=[None] * len(segs))
    embs = np.asarray(embedder(clip, [(s, e) for s, e, _ in groups]), dtype=np.float32)
    if embs.ndim != 2 or len(embs) != len(groups):
        raise ValueError("embedder returned the wrong shape")
    durs = np.array([e - s for s, e, _ in groups])
    labels = cluster(embs, durs, cluster_threshold)
    k = int(labels.max()) + 1
    x = normalize(embs)
    cents = normalize(np.stack([x[labels == c].mean(0) for c in range(k)]))
    names = match(cents, voiceprints, match_threshold, strangers, calibration, exclude, prior, match_margin,
                  extra=extra, split_cosine=split_cosine, min_samples=min_samples, wearer=wearer)

    order = []  # clusters in order of first appearance -> Speaker A, B, ...
    for c in labels:
        if c not in order:
            order.append(int(c))
    letters = iter(string.ascii_uppercase)
    tags = {}
    for c in order:
        tags[c] = names[c] or f"Speaker {next(letters, '?')}"

    clusters = []
    for c in range(k):
        members = np.flatnonzero(labels == c)
        best = members[np.argmax(durs[members])]
        s, e, idx = groups[best]
        if e - s > SNIPPET_MAX_S:
            mid = (s + e) / 2
            s, e = mid - SNIPPET_MAX_S / 2, mid + SNIPPET_MAX_S / 2
        excerpt = " ".join(segs[i][2] for i in idx)
        if len(excerpt) > 200:
            excerpt = excerpt[:200].rsplit(" ", 1)[0] + "…"
        clusters.append(Cluster(tag=tags[c], name=names[c], embedding=cents[c],
                                speech_s=float(durs[members].sum()), start_s=float(s),
                                end_s=float(e), excerpt=excerpt))

    seg_labels: list[str | None] = [None] * len(segs)
    for (s, e, idx), c in zip(groups, labels):
        for i in idx:
            seg_labels[i] = tags[int(c)]
    # segments too short to embed take the previous speaker (else the next)
    last = None
    for i in range(len(seg_labels)):
        if seg_labels[i] is None:
            seg_labels[i] = last
        last = seg_labels[i]
    nxt = None
    for i in reversed(range(len(seg_labels))):
        if seg_labels[i] is None:
            seg_labels[i] = nxt
        nxt = seg_labels[i]
    distinct = len({l for l in seg_labels if l})
    return Diarization(labels=seg_labels, clusters=clusters,
                       show=distinct >= 2 or any(c.name for c in clusters))


# ---------------------------------------------------------------------------
# word-level turns: windowed embeddings, a speaker per word

class Turn(NamedTuple):
    """Consecutive words of one speaker inside one whisper segment (so pauses and
    time stamps still apply per segment). A whole segment keeps whisper's text."""
    start: float
    end: float
    label: str | None
    text: str


# ---------------------------------------------------------------------------
# naming space: which model and which audio a voice is compared with voiceprints on

NAMING_MIN_S = 4.0      # whole-stretch naming: a voice with less speech has no naming embedding
PIECE_S = 20.0          # ...its speech is cut into pieces of at most this long, embedded whole
MAX_PIECES = 6          # ...at most this many (the ones with the most speech)
RUN_GAP_S = 0.5         # consecutive words of a voice this close are one stretch of audio


@dataclass
class Namer:
    """The space voices are named in (`speakers.embedder`, `speakers.naming`). `embedder` None is
    the clustering's own window embedder; `whole` embeds each voice's speech in long pieces (and
    each scored turn as one piece) instead of averaging 1.5 s windows. Namer() is ECAPA windows:
    the clustering centroids themselves, at no extra cost."""
    embedder: object = None
    whole: bool = False
    min_speech_s: float = NAMING_MIN_S
    piece_s: float = PIECE_S
    max_pieces: int = MAX_PIECES

    @property
    def is_default(self) -> bool:
        return self.embedder is None and not self.whole


def voice_pieces(words, labels, c: int, piece_s: float = PIECE_S, max_pieces: int = MAX_PIECES) -> list:
    """Voice c's speech as pieces to embed whole: [[(start, end), ...]] whose spans add up to at most
    piece_s each. Its consecutive words (no other voice's word between, gaps under RUN_GAP_S) are one
    stretch; stretches fill pieces in time order (a longer one is split); the max_pieces pieces with
    the most speech are kept, in time order."""
    spans: list[list[float]] = []
    prev = None
    for i, w in enumerate(words):
        if labels[i] != c:
            prev = None
            continue
        if prev is not None and w[0] - spans[-1][1] <= RUN_GAP_S:
            spans[-1][1] = max(spans[-1][1], w[1])
        else:
            spans.append([w[0], w[1]])
        prev = i
    pieces: list[list[tuple]] = [[]]
    room = piece_s
    for a, b in spans:
        while b - a > 1e-6:
            take = min(b - a, room)
            pieces[-1].append((a, a + take))
            a += take
            room -= take
            if room <= 1e-6:
                pieces.append([])
                room = piece_s
    pieces = [p for p in pieces if p]
    if len(pieces) > max_pieces:
        keep = sorted(sorted(range(len(pieces)), key=lambda k: -sum(e - s for s, e in pieces[k]))[:max_pieces])
        pieces = [pieces[k] for k in keep]
    return pieces


def embed_pieces(embedder, clip: Path, pieces: list, audio: np.ndarray | None = None) -> np.ndarray:
    """One embedding per piece ([[(start, end), ...]]): its spans' audio joined and embedded as one
    utterance. An embedder without `embed_wavs` (tests' fakes) embeds each span and the piece is
    their speech-weighted mean."""
    if hasattr(embedder, "embed_pieces"):
        return np.asarray(embedder.embed_pieces(clip, pieces, audio), dtype=np.float32)
    if hasattr(embedder, "embed_wavs"):
        audio = load_audio(clip) if audio is None else audio
        wavs = [np.concatenate([audio[int(a * SAMPLE_RATE):int(b * SAMPLE_RATE)] for a, b in p]) for p in pieces]
        return np.asarray(embedder.embed_wavs(wavs), dtype=np.float32)
    out = []
    for p in pieces:
        e = normalize(np.asarray(embedder(clip, p), dtype=np.float32))
        w = np.array([b - a for a, b in p])[:, None]
        out.append((e * w).sum(0))
    return np.stack(out).astype(np.float32)


def naming_embeddings(namer: Namer, clip: Path, words, labels, k: int, windows, wlab, x, audio=None, nx=None):
    """(embeddings [k, d] unit, named [k] bool): each voice's embedding in the naming space. Window
    kind: the mean of its windows (the clustering's own, or re-embedded with namer.embedder). Whole
    kind: the unit mean of its pieces (voice_pieces), embedded whole; a voice with under
    namer.min_speech_s of speech has none and is never named by voiceprint."""
    if not namer.whole:
        if nx is None:
            nx = x if namer.embedder is None else normalize(np.asarray(namer.embedder(clip, windows), dtype=np.float32))
        return normalize(np.stack([nx[wlab == c].mean(0) for c in range(k)])), np.ones(k, dtype=bool)
    emb = namer.embedder
    out, ok = [], np.zeros(k, dtype=bool)
    dim = None
    for c in range(k):
        speech = sum(w[1] - w[0] for i, w in enumerate(words) if labels[i] == c)
        pieces = voice_pieces(words, labels, c, namer.piece_s, namer.max_pieces) if speech >= namer.min_speech_s else []
        if pieces:
            e = normalize(embed_pieces(emb, clip, pieces, audio))
            out.append(normalize(e.mean(0)))
            dim = len(out[-1])
            ok[c] = True
        else:
            out.append(None)
    if dim is None:
        return np.zeros((k, 1), dtype=np.float32), ok
    return np.stack([v if v is not None else np.zeros(dim, np.float32) for v in out]), ok


def voice_windows(words, win_s: float = WINDOW_S, hop_s: float = HOP_S,
                  max_windows: int = MAX_WINDOWS):
    """Sliding windows over the voiced parts of a clip: ([(start, end)], voiced_s)
    where voiced_s is the word time inside each window. Windows with under
    MIN_WINDOW_VOICED_S of words are skipped, so silence is never embedded. The
    hop never exceeds win_s (no unsampled gaps): a clip needing more than
    max_windows windows raises ValueError and the caller uses segment turns."""
    hop_s = min(hop_s, win_s)
    ws = np.array([w[0] for w in words], dtype=np.float64)
    we = np.array([w[1] for w in words], dtype=np.float64)
    t0, t1 = float(ws.min()), float(we.max())
    span = max(t1 - t0 - win_s, 0.0)
    starts = t0 + np.arange(int(span // hop_s) + 1) * hop_s
    if span > 0 and starts[-1] < t0 + span - 1e-9:
        starts = np.append(starts, t0 + span)   # a last window flush with the end
    voiced = np.array([np.clip(np.minimum(we, t + win_s) - np.maximum(ws, t), 0, None).sum()
                       for t in starts])
    keep = voiced >= MIN_WINDOW_VOICED_S
    if keep.sum() > max_windows:
        raise ValueError(f"{int(keep.sum())} windows exceeds the cap of {max_windows}")
    return [(float(t), float(t + win_s)) for t in starts[keep]], voiced[keep]


def assign_words(words, windows, win_labels) -> list[int]:
    """The cluster of each word: windows covering the word's midpoint vote, each
    weighted by how close the word is to that window's centre (so the window
    centred on the word decides a voice change); a word no window covers takes
    the nearest window's cluster."""
    cents = np.array([(a + b) / 2 for a, b in windows])
    half = (windows[0][1] - windows[0][0]) / 2
    out = []
    for s, e, _w in words:
        m = (s + e) / 2
        dist = np.abs(cents - m)
        near = np.flatnonzero(dist < half)
        if not len(near):
            out.append(int(win_labels[int(np.argmin(dist))]))
            continue
        votes: dict[int, float] = {}
        for j in near:
            votes[int(win_labels[j])] = votes.get(int(win_labels[j]), 0.0) + (1.0 - dist[j] / half) + 1e-6
        out.append(max(votes, key=votes.get))
    return out


def smooth_runs(words, labels, min_run_s: float = MIN_RUN_S) -> list[int]:
    """Absorb voice changes shorter than min_run_s. The shortest short run goes
    first: between two runs of the same other speaker it joins them; between two
    different speakers it joins the longer neighbour; at an edge it joins its only
    neighbour. Repeats until every run is long enough (or only one run is left)."""
    runs: list[list[int]] = []   # [label, first word, last word]
    for i, l in enumerate(labels):
        if runs and runs[-1][0] == l:
            runs[-1][2] = i
        else:
            runs.append([l, i, i])

    def dur(r):   # speech time: silence between words doesn't lengthen a run
        return sum(words[i][1] - words[i][0] for i in range(r[1], r[2] + 1))
    while len(runs) > 1:
        k = min(range(len(runs)), key=lambda j: dur(runs[j]))
        if dur(runs[k]) >= min_run_s:
            break
        prev, nxt = runs[k - 1] if k else None, runs[k + 1] if k + 1 < len(runs) else None
        if prev and nxt and prev[0] == nxt[0]:
            prev[2] = nxt[2]
            del runs[k:k + 2]
        else:
            into = prev if not nxt else nxt if not prev else (prev if dur(prev) >= dur(nxt) else nxt)
            into[1], into[2] = min(into[1], runs[k][1]), max(into[2], runs[k][2])
            del runs[k]
        # neighbours that now carry the same label fuse
        j = 1
        while j < len(runs):
            if runs[j][0] == runs[j - 1][0]:
                runs[j - 1][2] = runs[j][2]
                del runs[j]
            else:
                j += 1
    out = [0] * len(labels)
    for l, a, b in runs:
        for i in range(a, b + 1):
            out[i] = l
    return out


@dataclass
class Boundaries:
    """Turn-boundary settings (`speakers.*` in config.json, process.boundary_settings). Each
    pass has its own switch; off, turns are what clustering and rescoring make of them."""
    min_run_s: float = MIN_RUN_S    # a voice change with less speech than this is absorbed (smooth_runs)
    # blip absorption: a run of at most blip_words words between two runs of one other voice
    # joins them unless its own windows favour its voice over theirs by blip_margin (cosine)
    blip_absorb: bool = False
    blip_words: int = 3
    blip_margin: float = 0.1
    # with blip_absorb, a run with less speech than short_run_s (but at least min_run_s) joins a
    # neighbour by the same evidence rule: the voice either side when both are one voice, else
    # the longer neighbour (0 = off)
    short_run_s: float = 0.0
    # change points: inside a run, where the mean of the cp_side_s of windows before a point and
    # the mean after it are less alike than cp_threshold (cosine), the run is split there
    change_points: bool = False
    cp_side_s: float = 2.0
    cp_threshold: float = 0.3
    cp_same: float = 0.5            # a split-off piece this alike a voice's centroid is that voice
    # boundary snapping: a voice change moves by at most snap_words words to just after a
    # sentence end (or comma, or a clear pause) when where it fell has none (0 = off)
    snap_words: int = 0


def _runs(labels) -> list[list]:
    """[[label, first index, last index]] of consecutive equal labels."""
    runs: list[list] = []
    for i, lab in enumerate(labels):
        if runs and runs[-1][0] == lab:
            runs[-1][2] = i
        else:
            runs.append([lab, i, i])
    return runs


def _span_embedding(words, a: int, z: int, wcent: np.ndarray, x: np.ndarray, pad: float = 0.0):
    """Unit mean of the windows centred inside words a..z (widened by pad), else the nearest window."""
    inside = (wcent >= words[a][0] - pad) & (wcent <= words[z][1] + pad)
    if not inside.any():
        inside = np.zeros(len(wcent), dtype=bool)
        inside[int(np.argmin(np.abs(wcent - (words[a][0] + words[z][1]) / 2)))] = True
    return normalize(x[inside].mean(0))


def change_points(words, labels, windows, x, cents, names, b: Boundaries):
    """Split runs where the voice changes without clustering noticing: (labels, centroids,
    names, info). Inside each run the windows are walked in order, and at each point the mean
    of the cp_side_s of windows before it is compared with the mean after it; where the least
    alike pair falls under cp_threshold the run splits at the widest word gap between those two
    windows, and both halves are looked at again. Then, per voice, the pieces of its split runs
    are grouped (average linkage at cp_same): the group most like the voice's centroid keeps the
    voice, and every other group goes to another voice of the conversation when it is at least
    cp_same like it and more like it than its own, else becomes a new, unnamed voice (turn
    rescoring gives it to a person when its turns score high enough). Voices that lost or
    gained speech get the centroid of the windows now theirs."""
    wcent = np.array([(s + e) / 2 for s, e in windows])
    n_side = max(2, int(np.ceil(b.cp_side_s / HOP_S)))
    labels = list(labels)
    cents = [normalize(c) for c in np.asarray(cents)]
    names = list(names)
    n0 = len(cents)
    info = {"splits": 0, "to_voice": 0, "new_voices": 0}
    pieces: dict[int, list] = {}      # voice -> [(first word, last word, unit embedding)] of its split runs
    for c, a, z in _runs(labels):
        todo, done = [(a, z)], []
        while todo:
            a_, z_ = todo.pop()
            idx = np.flatnonzero((wcent >= words[a_][0]) & (wcent <= words[z_][1]))
            if len(idx) < 2 * n_side or z_ <= a_:
                done.append((a_, z_))
                continue
            sims = [float(normalize(x[idx[k - n_side:k]].mean(0)) @ normalize(x[idx[k:k + n_side]].mean(0)))
                    for k in range(n_side, len(idx) - n_side + 1)]
            k = int(np.argmin(sims)) + n_side
            if sims[k - n_side] >= b.cp_threshold:
                done.append((a_, z_))
                continue
            lo, hi = wcent[idx[k - 1]], wcent[idx[k]]
            cand = [i for i in range(a_ + 1, z_ + 1) if lo <= words[i][0] <= hi]
            if not cand:
                cand = [min(range(a_ + 1, z_ + 1), key=lambda i: abs(words[i][0] - (lo + hi) / 2))]
            i = max(cand, key=lambda i: words[i][0] - words[i - 1][1])
            todo += [(a_, i - 1), (i, z_)]
            info["splits"] += 1
        if len(done) > 1:
            pieces.setdefault(c, []).extend((a_, z_, _span_embedding(words, a_, z_, wcent, x)) for a_, z_ in done)
    changed = set()
    for c, ps in pieces.items():
        groups = agglomerative(np.stack([e for _a, _z, e in ps]), b.cp_same)
        gc = [normalize(np.mean([e for (_a, _z, e), g in zip(ps, groups) if g == k], 0))
              for k in range(int(groups.max()) + 1)]
        keep = int(np.argmax([v @ cents[c] for v in gc]))
        for k, v in enumerate(gc):
            if k == keep:
                continue
            others = [(float(v @ cents[o]), o) for o in range(n0) if o != c]
            best = max(others, default=(-np.inf, None))
            if best[1] is not None and best[0] >= b.cp_same and best[0] > float(v @ cents[c]):
                to = best[1]
                info["to_voice"] += 1
            else:
                to = len(cents)
                cents.append(v)
                names.append(None)
                info["new_voices"] += 1
            for (a_, z_, _e), g in zip(ps, groups):
                if g == k:
                    labels[a_:z_ + 1] = [to] * (z_ - a_ + 1)
            changed |= {c, to}
    for c in changed:
        mine = np.zeros(len(windows), dtype=bool)
        for i, lab in enumerate(labels):
            if lab == c:
                mine |= (wcent >= words[i][0] - HOP_S / 2) & (wcent <= words[i][1] + HOP_S / 2)
        if mine.any():
            cents[c] = normalize(x[mine].mean(0))
    return labels, np.stack(cents), names, info


EVIDENCE_SHARE = 0.5   # a window speaks for a run only when at least this share of it lies inside the run


def _run_evidence(words, a: int, z: int, windows, x: np.ndarray):
    """Unit mean of the windows with at least EVIDENCE_SHARE of their span inside words a..z
    (their start to end), or None: no window is mostly this run's audio."""
    s, e = words[a][0], words[z][1]
    w = np.asarray(windows, dtype=np.float64)
    inside = np.clip(np.minimum(w[:, 1], e) - np.maximum(w[:, 0], s), 0, None)
    mine = inside >= EVIDENCE_SHARE * (w[:, 1] - w[:, 0])
    return normalize(x[mine].mean(0)) if mine.any() else None


def absorb_blips(words, labels, windows, x, cents, b: Boundaries):
    """Short runs join their neighbours unless their own audio says otherwise:
    - A–B–A blips: a run of at most b.blip_words words between two runs of one other voice;
    - short runs: speech under b.short_run_s, into the voice either side when both are one,
      else the longer neighbour (by speech), else the only one.
    A run is absorbed only when windows mostly inside it (EVIDENCE_SHARE) exist and don't favour
    its own voice's centroid over the target's by b.blip_margin; with no such window it is kept
    (missing evidence never takes a reply away). Returns (labels, info {absorbed, kept,
    no_evidence})."""
    labels = list(labels)
    info = {"absorbed": 0, "kept": 0, "no_evidence": 0}

    def speech(a, z):
        return sum(words[i][1] - words[i][0] for i in range(a, z + 1))
    runs = _runs(labels)
    for j in range(len(runs)):
        c, a, z = runs[j]
        left = labels[runs[j - 1][2]] if j else None
        right = labels[runs[j + 1][1]] if j + 1 < len(runs) else None
        target = None
        if left is not None and left == right and left != c and z - a + 1 <= b.blip_words:
            target = left
        elif b.short_run_s > 0 and (left is not None or right is not None) and speech(a, z) < b.short_run_s:
            if left is not None and left == right:
                target = left
            elif left is None or right is None:
                target = right if left is None else left
            else:
                ls = speech(runs[j - 1][1], runs[j - 1][2])
                rs = speech(runs[j + 1][1], runs[j + 1][2])
                target = left if ls >= rs else right
        if target is None or target == c:
            continue
        e = _run_evidence(words, a, z, windows, x)
        if e is None:
            info["no_evidence"] += 1
            continue
        if float(e @ normalize(cents[c]) - e @ normalize(cents[target])) >= b.blip_margin:
            info["kept"] += 1
            continue
        labels[a:z + 1] = [target] * (z - a + 1)
        info["absorbed"] += 1
    return labels, info


SNAP_PAUSE_S = 0.4   # a gap between words at least this long is a pause a voice change can sit in


def _break_strength(words, p: int, capital: bool = False) -> tuple[int, int]:
    """How natural a voice change before word p is: (2 when word p-1 ends a sentence (. ? !),
    1 for a comma, colon or semicolon, else 0; then 1 when the gap before p is a pause). With
    `capital`, Whisper capitalizing word p (not "I", I'm, ...) counts as a sentence start too:
    used only for where a change already is, since a capital mid-sentence is often a name."""
    t = words[p - 1][2].rstrip("\"')\u201d\u2019 ")
    punct = 2 if t.endswith((".", "?", "!", "\u2026")) else 1 if t.endswith((",", ";", ":")) else 0
    if capital and punct < 2:
        nxt = words[p][2].lstrip("\"'(\u201c\u2018 ")
        if nxt[:1].isupper() and nxt.split("'")[0].split("\u2019")[0] != "I":
            punct = 2
    return punct, int(words[p][0] - words[p - 1][1] >= SNAP_PAUSE_S)


def snap_boundaries(words, labels, max_words: int):
    """Move each voice change by at most max_words words to the most natural break near it
    (_break_strength: after a sentence end, then a comma, then a pause; nearest first), when
    where it fell is less natural (a capitalized sentence start where it fell counts as natural).
    The voice windows are 1.5 s wide, so a change found by voice alone is often a word early or
    late ("...what I said. I | don't believe you"). Every run
    keeps at least one word, and no change crosses another. Returns (labels, info {moved})."""
    labels = list(labels)
    info = {"moved": 0}
    if max_words <= 0:
        return labels, info
    runs = _runs(labels)
    for j in range(1, len(runs)):
        b = runs[j][1]
        lo = max(runs[j - 1][1] + 1, b - max_words)
        hi = min(runs[j][2], b + max_words)
        here = _break_strength(words, b, capital=True)
        best = max(range(lo, hi + 1), key=lambda p: (_break_strength(words, p), -abs(p - b)))
        if best == b or _break_strength(words, best) <= here:
            continue
        if best < b:
            labels[best:b] = [runs[j][0]] * (b - best)
        else:
            labels[b:best] = [runs[j - 1][0]] * (best - b)
        runs[j][1] = best
        runs[j - 1][2] = best - 1
        info["moved"] += 1
    return labels, info


def _absorb_or_keep(words, labels, windows, x, cents, b: Boundaries, passes: dict):
    """absorb_blips, or the labels unchanged if it fails (passes["blips"] says which); then
    snap_boundaries (passes["snap"]), likewise never at the cost of the turns."""
    if b.blip_absorb:
        try:
            labels, passes["blips"] = absorb_blips(words, labels, windows, x, cents, b)
        except Exception as e:
            passes["blips"] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
            labels = list(labels)
    if b.snap_words > 0:
        try:
            labels, passes["snap"] = snap_boundaries(words, labels, b.snap_words)
        except Exception as e:
            passes["snap"] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
    return list(labels)


def diarize_words(clip: Path, segs, embedder, voiceprints: dict, *, cluster_threshold: float,
                  match_threshold: float, strangers=None, calibration=None,
                  exclude=None, prior=None, match_margin: float = 0.0, extra=None,
                  split_cosine: float | None = None, min_samples: int = 0, wearer: str | None = None,
                  rescorer: TurnScorer | None = None,
                  keep_before: bool = False, boundaries: Boundaries | None = None,
                  namer: Namer | None = None) -> Diarization:
    """Like diarize, but the voice is followed inside segments: windows of
    WINDOW_S every HOP_S over the voiced parts are embedded and clustered (same
    threshold, floor and voiceprint matching), each word takes its windows'
    cluster, short flickers are absorbed, and turns start where the speaker
    changes. `boundaries` (Boundaries) sets the flicker length and switches the
    turn-boundary passes: change points inside long turns (before rescoring, so
    a split-off half is rescored like any turn) and blip absorption (after). Each
    pass that fails leaves the turns as they were (`Diarization.boundaries` says
    why). With `rescorer`, every turn is then scored on its own audio and may
    move to another voice or person (rescore_turns); if that fails, the cluster-level
    labels stand and `Diarization.rescore` says why. keep_before: also return the
    cluster-level turns (`Diarization.before`, for the eval). `min_samples` changes nothing in
    naming or rescoring: at the end, every voice named after a person with fewer samples (not the
    `wearer`) is left unnamed. Raises when word timing is missing or embedding fails; the caller
    falls back to diarize()."""
    bnd = boundaries or Boundaries()
    if any(seg[2].strip() and not getattr(seg, "words", None) for seg in segs):
        raise ValueError("a segment has no word timestamps")
    flat = [(w, i) for i, seg in enumerate(segs) for w in (getattr(seg, "words", None) or ())]
    if not flat:
        raise ValueError("no word timestamps")
    flat.sort(key=lambda p: p[0][0])
    words = [w for w, _i in flat]
    wseg = [i for _w, i in flat]
    windows, voiced = voice_windows(words)
    if not windows:
        raise ValueError("no voiced windows")
    embs = np.asarray(embedder(clip, windows), dtype=np.float32)
    if embs.ndim != 2 or len(embs) != len(windows):
        raise ValueError("embedder returned the wrong shape")
    durs = voiced * (HOP_S / WINDOW_S)   # overlapping windows: count each second of speech once
    wlab = cluster(embs, durs, cluster_threshold, max_n=400)
    k = int(wlab.max()) + 1
    x = normalize(embs)
    cents = normalize(np.stack([x[wlab == c].mean(0) for c in range(k)]))
    word_cluster = smooth_runs(words, assign_words(words, windows, wlab), bnd.min_run_s)
    namer = namer or Namer()
    if namer.whole and namer.embedder is None:
        import dataclasses
        namer = dataclasses.replace(namer, embedder=embedder)
    audio_cache: dict = {}

    def clip_audio():   # the clip's audio, loaded once, for embedding joined pieces
        if "a" not in audio_cache:
            audio_cache["a"] = load_audio(clip) if hasattr(namer.embedder, "embed_wavs") else None
        return audio_cache["a"]
    nx = None
    if namer.is_default:
        ncents, nok = cents, np.ones(k, dtype=bool)
    else:
        calibration = None      # top-k / s-norm samples live in the ECAPA window space: mean scoring only
        if not namer.whole:
            nx = normalize(np.asarray(namer.embedder(clip, windows), dtype=np.float32))
        ncents, nok = naming_embeddings(namer, clip, words, word_cluster if namer.whole else wlab, k, windows, wlab,
                                        x, clip_audio() if namer.whole else None, nx=nx)
    names = [None] * k
    if nok.any():
        idx = np.flatnonzero(nok)
        for i, n in zip(idx, match(ncents[idx], voiceprints, match_threshold, strangers, calibration, exclude, prior,
                                   match_margin, extra=extra, split_cosine=split_cosine)):
            names[int(i)] = n
    if rescorer is not None and not namer.is_default:
        import dataclasses
        rescorer = dataclasses.replace(rescorer, namer=namer, namer_audio=clip_audio() if namer.whole else None,
                                       namer_clip=clip, namer_x=nx, calibration=None)

    def bare(ns):   # thin people's voices unnamed (speakers.min_samples), nothing else changed
        return unname_thin(ns, thin_names(ns, voiceprints, calibration, min_samples, wearer))

    passes: dict = {}
    before = None
    if keep_before:
        before = _labelled(segs, words, wseg, word_cluster, bare(names), cents, set())
    if bnd.change_points:
        try:
            word_cluster, cents, names, passes["change_points"] = change_points(
                words, word_cluster, windows, x, cents, names, bnd)
        except Exception as e:  # never worse than the clustering's turns
            passes["change_points"] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
    if rescorer is None:
        word_cluster = _absorb_or_keep(words, word_cluster, windows, x, cents, bnd, passes)
        out = _labelled(segs, words, wseg, word_cluster, bare(names), cents, set())
        out.before = before.turns if keep_before and before else None
        out.boundaries = passes or None
        return out
    import time
    t0 = time.process_time()
    audio, audio_error = None, None
    if rescorer.features:
        try:   # the near-field features need the waveform; without it rescoring runs as before
            audio = (rescorer.audio_loader or load_audio)(clip)
        except Exception as e:
            audio_error = f"{type(e).__name__}: {str(e)[:80]}"
    t_audio = time.process_time() - t0
    try:
        new_cluster, cents2, names2, info = rescore_turns(words, wseg, word_cluster, windows, x, cents, names,
                                                          rescorer, **({"audio": audio} if audio is not None else {}))
        new_cluster = _absorb_or_keep(words, new_cluster, windows, x, cents2, bnd, passes)
        out = _labelled(segs, words, wseg, new_cluster, bare(names2), cents2, set(info["mixed"]))
        tag_of = {c.index: c.tag for c in out.clusters}
        old = _labelled(segs, words, wseg, word_cluster, bare(names), cents, set())
        tag_of.update({c.index: c.tag for c in old.clusters if c.index not in tag_of})
        info["moves"] = {f"{tag_of.get(a, '?')} -> {tag_of.get(b, '?')}": n for (a, b), n in info["moves"].items()}
        info["mixed"] = sorted(tag_of.get(c, "?") for c in info["mixed"])
        for t in info.get("turns", ()):
            t["voice"] = tag_of.get(t["voice"], "?")
        w = info.get("wearer")
        if w and "p" in w:
            for c in out.clusters:
                c.wearer_named = c.index in w["named"]
            w["p"] = [[tag_of.get(c, "?"), p] for c, p in w["p"].items()]
            w["named"] = sorted(tag_of.get(c, "?") for c in w["named"])
            w["unnamed"] = sorted(tag_of.get(c, "?") for c in w["unnamed"])
        if audio_error:
            info["features_error"] = audio_error
        if audio is not None:
            info["features_cpu_s"] = round(t_audio + info.pop("_features_cpu_s", 0.0), 3)
    except Exception as e:  # never worse than the cluster-level labels
        word_cluster = _absorb_or_keep(words, word_cluster, windows, x, cents, bnd, passes)
        out = _labelled(segs, words, wseg, word_cluster, bare(names), cents, set())
        info = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
    info["cpu_s"] = time.process_time() - t0
    out.rescore = info
    out.before = before.turns if keep_before and before else None
    out.boundaries = passes or None
    return out


def _labelled(segs, words, wseg, word_cluster, names, cents, mixed: set) -> Diarization:
    """The Diarization for a cluster per word: tags by first appearance (Speaker A, B, ...
    for unnamed voices), one Cluster per voice still speaking, and the turn pieces."""
    order: list[int] = []      # clusters still speaking, by first appearance -> Speaker A, B, ...
    for c in word_cluster:
        if c not in order:
            order.append(c)
    letters = iter(string.ascii_uppercase)
    tags = {c: names[c] or f"Speaker {next(letters, '?')}" for c in order}

    # Each cluster's snippet is its longest run of consecutive words.
    runs: dict[int, tuple[float, int, int]] = {}
    speech = {c: 0.0 for c in order}
    i = 0
    while i < len(words):
        j = i
        while j + 1 < len(words) and word_cluster[j + 1] == word_cluster[i]:
            j += 1
        c = word_cluster[i]
        speech[c] += sum(words[t][1] - words[t][0] for t in range(i, j + 1))
        length = words[j][1] - words[i][0]
        if c not in runs or length > runs[c][0]:
            runs[c] = (length, i, j)
        i = j + 1
    clusters = []
    for c in order:
        _l, a, b = runs[c]
        s, e = words[a][0], words[b][1]
        if e - s > SNIPPET_MAX_S:
            mid = (s + e) / 2
            s, e = mid - SNIPPET_MAX_S / 2, mid + SNIPPET_MAX_S / 2
        excerpt = " ".join(w[2] for w in words[a:b + 1])
        if len(excerpt) > 200:
            excerpt = excerpt[:200].rsplit(" ", 1)[0] + "…"
        clusters.append(Cluster(tag=tags[c], name=names[c], embedding=cents[c], speech_s=float(speech[c]),
                                start_s=float(s), end_s=float(e), excerpt=excerpt, mixed=c in mixed, index=c))

    # Pieces: consecutive same-speaker words within one segment.
    by_seg: dict[int, list[int]] = {}
    for n, si in enumerate(wseg):
        by_seg.setdefault(si, []).append(n)
    pieces: list[list] = []   # [seg, start, end, label, text]
    for si, seg in enumerate(segs):
        idx = by_seg.get(si)
        if not idx:
            pieces.append([si, seg[0], seg[1], None, seg[2]])
            continue
        groups: list[list[int]] = []
        for n in idx:
            if groups and word_cluster[groups[-1][-1]] == word_cluster[n]:
                groups[-1].append(n)
            else:
                groups.append([n])
        for g in groups:
            text = seg[2] if len(groups) == 1 else " ".join(words[n][2] for n in g)
            pieces.append([si, words[g[0]][0], words[g[-1]][1], tags[word_cluster[g[0]]], text])
    last = None                # word-less segments take the previous speaker (else the next)
    for p in pieces:
        p[3] = p[3] or last
        last = p[3]
    nxt = None
    for p in reversed(pieces):
        p[3] = p[3] or nxt
        nxt = p[3]
    turns = [Turn(s, e, l, t) for _si, s, e, l, t in pieces]

    seg_time: list[dict] = [dict() for _ in segs]   # a segment's label: who speaks most of it
    for n, si in enumerate(wseg):
        t = tags[word_cluster[n]]
        seg_time[si][t] = seg_time[si].get(t, 0.0) + words[n][1] - words[n][0]
    seg_labels: list[str | None] = [max(d, key=d.get) if d else None for d in seg_time]
    for lst in (range(len(seg_labels)), reversed(range(len(seg_labels)))):
        prev = None
        for i in lst:
            seg_labels[i] = seg_labels[i] or prev
            prev = seg_labels[i]
    distinct = len({t.label for t in turns if t.label})
    return Diarization(labels=seg_labels, clusters=clusters, turns=turns,
                       show=distinct >= 2 or any(c.name for c in clusters))


# ---------------------------------------------------------------------------
# turn rescoring: a second look at each turn, after clustering and naming

TURN_MIN_S = 2.0       # a turn with less speech than this isn't judged on its own
TURN_PERSON_FLOOR = 0.4   # raw cosine a turn needs with a person to move from one named voice to them
MIXED_KEEP = 0.6       # a voice keeping less of its speech than this after rescoring is mixed
VOICE = "#voice:"      # score_matrix column of one of the conversation's own voices


@dataclass
class TurnScorer:
    """How turns are rescored: the same voiceprints, scorer (`speakers.scoring`) and
    strangers as cluster naming, plus the margins a move needs. `margin` is in cosine
    units (mean and topk scores, and calibrated people on the raw fallback),
    `margin_calibrated` in s-norm units (both sides on the calibrated scale). A turn of a
    voice named after a person moves to another person when it beats the first by the
    margin and reaches `person_floor` (raw cosine; None: the match threshold): one turn's
    few windows score lower against a voiceprint than a whole voice does, and the question
    is which of two people, not whether it is anyone at all."""
    voiceprints: dict
    threshold: float = 0.5
    calibration: object = None
    strangers: np.ndarray | None = None
    exclude: object = None
    margin: float = 0.1
    margin_calibrated: float = 1.0
    person_floor: float | None = TURN_PERSON_FLOOR
    prior: dict | None = None       # closeness prior ({name: float}), the one cluster naming used
    extra: dict | None = None       # context bars ({lowercased name: cosine}), the ones cluster naming used
    split_cosine: float | None = None   # speakers.split_voice_cosine, as cluster naming used it
    namer: Namer | None = None          # the naming space: people are scored on turns embedded in it
    namer_audio: object = None          # the clip's audio for whole-turn embedding
    namer_clip: object = None
    namer_x: object = None              # window kind: every window re-embedded in the naming space
    min_s: float = TURN_MIN_S
    mixed_keep: float = MIXED_KEEP
    # near-field wearer signal (wearer.py): features are measured on every scored turn when
    # the clip's audio loads; `wearer_model` (wearer.Model, None = unfitted) then names voices
    wearer: str = "Me"
    features: bool = True
    audio_loader: object = None     # clip -> float32 audio at 16 kHz (default: load_audio)
    device: str | None = None
    device_stats: dict | None = None    # this pendant's stored-turn stats (wearer.stats)
    wearer_model: object = None
    wearer_p: float = 0.9
    wearer_strict: float = 0.65             # raw score a far-sounding voice needs to stay the wearer...
    wearer_strict_calibrated: float = 2.5   # ...or calibrated score
    wearer_min_s: float = 5.0       # a voice needs this much speech before the wearer signal renames it

    def matrix(self, x: np.ndarray, cents: np.ndarray, px: np.ndarray | None = None) -> ScoreMatrix:
        """Scores of each turn against every person and every voice of the conversation
        (`VOICE<i>`, one sample: the voice's centroid), through score_matrix. With `px` (the turns
        in the naming space, Namer) people are scored on px and voices on x, mean scoring only."""
        voices = {f"{VOICE}{c}": normalize(cents[c]) for c in range(len(cents))}
        if px is not None:
            p = score_matrix(px, self.voiceprints, self.threshold)
            v = score_matrix(x, {k: (e, 1) for k, e in voices.items()}, self.threshold)
            return ScoreMatrix(p.names + v.names, *(np.hstack([a, b]) for a, b in
                                                    zip(p[1:], v[1:])))
        cal = self.calibration
        if cal is not None and getattr(cal, "names", None):
            import dataclasses
            samples = {**cal.samples, **{k: (v[None, :], [None]) for k, v in voices.items()}}
            return score_matrix(x, {}, self.threshold, dataclasses.replace(cal, samples=samples),
                                exclude=self.exclude)
        return score_matrix(x, {**self.voiceprints, **{k: (v, 1) for k, v in voices.items()}}, self.threshold)


def turn_units(words, wseg, word_cluster) -> list[tuple[int, int]]:
    """[(first word, last word)] of each turn: consecutive words of one voice inside
    one whisper segment (the pieces the transcript shows)."""
    out: list[tuple[int, int]] = []
    for i in range(len(words)):
        if out and word_cluster[i] == word_cluster[out[-1][1]] and wseg[i] == wseg[out[-1][1]]:
            out[-1] = (out[-1][0], i)
        else:
            out.append((i, i))
    return out


def rescore_turns(words, wseg, word_cluster, windows, x, cents, names, scorer: TurnScorer, audio=None):
    """Move turns that are clearly someone else: (word clusters, centroids, names, info).

    Each turn with at least scorer.min_s of speech is embedded as the mean of the window
    embeddings centred inside it (no new audio is embedded) and scored against every voice
    of the conversation (its centroid) and every voiceprint person (TurnScorer.matrix).
    Like is compared with like, and a target must reach its match threshold:
    - another voice: the turn's score for it minus its score for its own voice;
    - a person (not the one its voice is named after, and not closer to a stranger): the
      score minus the score for the person its voice is named after or, for an unnamed
      voice, minus the best other person's and the threshold. If the person already is a
      voice here, the turn must not sound more like its own voice than that one.
    Differences are in the scorer's units: s-norm when every score involved is calibrated
    (margin_calibrated), else raw cosine (margin). A move needs at least one margin; the
    target with the most margins wins, and a person who isn't a voice yet becomes a new
    voice named after them. A shorter turn follows its nearest turns of the same voice
    when they moved (the nearer one when the two sides disagree), else stays. Voices
    keeping under scorer.mixed_keep of their speech under their own label (but some) are
    mixed; speech moved to another voice named after the same person still counts as kept.
    With `audio` (the clip, 16 kHz) every scored turn also gets near-field features
    (wearer.py) and its score for the wearer; with a wearer model, then wearer_pass.
    info: scored, moved (turns), moved_s, inherited, moves {(from, to): turns}, mixed, and
    turns [{start, end, speech_s, voice, level_db, decay_db, flatness, score}] when
    features were measured, wearer {voice: p} when a model ran."""
    wc = word_cluster
    wcent = np.array([(a + b) / 2 for a, b in windows])
    units = turn_units(words, wseg, wc)
    speech = [sum(words[i][1] - words[i][0] for i in range(a, b + 1)) for a, b in units]
    rows, embs = [], []
    for u, (a, b) in enumerate(units):
        if speech[u] < scorer.min_s:
            continue
        inside = (wcent >= words[a][0]) & (wcent <= words[b][1])
        if inside.any():
            rows.append(u)
            embs.append(normalize(x[inside].mean(0)))
    target_of: dict[int, object] = {}    # unit -> cluster index, or ("person", name)
    wscore: dict[int, tuple[float, bool]] = {}   # unit -> (wearer score over his threshold, calibrated)
    if rows:
        px = None
        nm = scorer.namer
        if nm is not None and not nm.is_default:     # people are scored on the turn in the naming space
            if nm.whole:
                px = normalize(embed_pieces(nm.embedder, scorer.namer_clip,
                                            [[(words[units[u][0]][0], words[units[u][1]][1])] for u in rows],
                                            scorer.namer_audio))
            else:
                nx = scorer.namer_x
                px = normalize(np.stack([nx[(wcent >= words[units[u][0]][0]) & (wcent <= words[units[u][1]][1])].mean(0)
                                         for u in rows]))
        m = scorer.matrix(np.stack(embs), cents, px)
        col = {n: j for j, n in enumerate(m.names)}
        wj = next((j for j, n in enumerate(m.names) if n.lower() == (scorer.wearer or "").lower()), None)
        if wj is not None:
            for r, u in enumerate(rows):
                if np.isfinite(m.match[r, wj]):
                    wscore[u] = (float(m.match[r, wj] - m.threshold[r, wj]), bool(m.calibrated[r, wj]))
        active = sorted(set(wc))
        people = [n for n in m.names if not n.startswith(VOICE)]
        near = np.full(len(rows), -np.inf)
        if scorer.strangers is not None and len(scorer.strangers):
            near = (np.stack(embs) @ normalize(scorer.strangers).T).max(axis=1)

        voices_of: dict[str, list[int]] = {}    # a name can have two voices (split_cosine)
        for c in active:
            if names[c]:
                voices_of.setdefault(names[c], []).append(c)
        cn = normalize(np.asarray(cents, dtype=np.float32))
        pv = closeness.vector(m.names, scorer.prior)

        def gain(r, j, refs, floor):
            """How many margins column j beats the best of `refs` (and, with floor, its own
            threshold) by: on the calibrated scale when every column involved is on it, else
            on raw cosines with the raw threshold."""
            if m.calibrated[r, j] and all(m.calibrated[r, i] for i in refs):
                vals, need, thr = m.match[r], scorer.margin_calibrated, m.threshold[r, j]
            else:
                vals, need, thr = m.raw[r], scorer.margin, scorer.threshold
            ref = max((vals[i] for i in refs), default=-np.inf)
            if floor:
                ref = max(ref, thr)
            return (vals[j] - ref) / max(need, 1e-6)

        def ok(r, j):
            return np.isfinite(m.match[r, j]) and m.match[r, j] >= m.threshold[r, j]

        bars = {k.lower(): float(v) for k, v in (scorer.extra or {}).items() if v}

        for r, u in enumerate(rows):
            own = wc[units[u][0]]
            own_v = col[f"{VOICE}{own}"]
            own_p = col.get(names[own]) if names[own] else None
            best, best_x = None, 0.0
            # another voice of this conversation, compared with the turn's own voice
            for c in active:
                j = col[f"{VOICE}{c}"]
                if c != own and ok(r, j):
                    x_ = gain(r, j, [own_v], False)
                    if x_ >= 1.0 and x_ > best_x:
                        best, best_x = c, x_
            # a person, compared with the person the turn's voice is named after; for an
            # unnamed voice, with everyone else and the person's own threshold
            moves_to = []          # (margins, prior-aware rank key, target)
            for q in people:
                j = col[q]
                if q == names[own] or m.raw[r, j] <= near[r]:
                    continue
                # turn against turn: a voice named after a person is relabelled by comparing two people,
                # so the person only needs to resemble the turn (person_floor); otherwise their threshold
                if not (ok(r, j) if own_p is None or scorer.person_floor is None
                        else np.isfinite(m.raw[r, j]) and m.raw[r, j] >= scorer.person_floor):
                    continue
                refs = [own_p] if own_p is not None else [col[p] for p in people if p != q]
                x_ = gain(r, j, refs, own_p is None)
                held = voices_of.get(q)
                target = max(held, key=lambda c: m.raw[r, col[f"{VOICE}{c}"]]) if held else ("person", q)
                # never against this conversation's own evidence: the turn must not sound more like
                # its own voice than like the voice already named q, unless its own voice is unnamed
                # and could share q's name (within split_cosine of that voice: one person split in two)
                if (isinstance(target, int) and m.raw[r, col[f"{VOICE}{target}"]] < m.raw[r, own_v]
                        and not (names[own] is None and _shares_name(cn, own, [target], scorer.split_cosine))):
                    continue
                if x_ >= 1.0:
                    moves_to.append((x_, closeness.pair_key(m.match[r, j], m.threshold[r, j],
                                                           None if pv is None else pv[j]), target))
            if moves_to:
                # the person speakers.match would pick: with a closeness prior, by margin above
                # each one's own threshold plus the prior; without, by how clearly they win
                x_, _k, target = max(moves_to, key=(lambda c: c[1]) if pv is not None else (lambda c: c[0]))
                if x_ > best_x:
                    best, best_x = target, x_
            if best is not None and bars:
                # a context bar on the winning person: the turn stays where it is (nobody else wins it)
                q = best[1] if isinstance(best, tuple) else names[best]
                bar = bars.get(str(q).lower(), 0.0) if q and q in col else 0.0
                if bar:
                    floor = scorer.person_floor if own_p is not None and scorer.person_floor is not None \
                        else scorer.threshold
                    if m.raw[r, col[q]] < floor + bar:
                        best = None
            if best is not None:
                target_of[u] = best
    # persons who aren't a voice yet become new voices
    names = list(names)
    new_of: dict[str, int] = {}
    for u, t in target_of.items():
        if isinstance(t, tuple):
            if t[1] not in new_of:
                new_of[t[1]] = len(names)
                names.append(t[1])
            target_of[u] = new_of[t[1]]
    out = list(wc)
    moves: dict[tuple[int, int], int] = {}
    moved_s = 0.0
    for u, t in target_of.items():
        a, b = units[u]
        for i in range(a, b + 1):
            out[i] = t
        moves[(wc[a], t)] = moves.get((wc[a], t), 0) + 1
        moved_s += speech[u]
    inherited = 0
    long_u = set(rows)
    for u, (a, b) in enumerate(units):
        if u in long_u:
            continue
        c = wc[a]
        picks = []
        for step in (-1, 1):
            v = u + step
            while 0 <= v < len(units) and wc[units[v][0]] == c and v not in long_u:
                v += step
            if 0 <= v < len(units) and wc[units[v][0]] == c:
                gap = words[a][0] - words[units[v][1]][1] if step < 0 else words[units[v][0]][0] - words[b][1]
                picks.append((gap, out[units[v][0]]))
        if not picks:
            continue
        if len(picks) == 2 and picks[0][1] == picks[1][1]:
            to = picks[0][1]
        else:
            to = min(picks)[1]
        if to != c:
            for i in range(a, b + 1):
                out[i] = to
            inherited += 1
    # new voices, and voices that lost or gained turns: the mean of the windows now theirs
    # (a voice's stored embedding is what Voice Scoring and enrollment use)
    cents = list(cents) + [None] * len(new_of)
    changed = {c for u, t in target_of.items() for c in (wc[units[u][0]], t)}
    changed |= {c for i, c in enumerate(out) if c != wc[i]}
    for c in sorted(changed):
        mine = np.zeros(len(windows), dtype=bool)
        for i, lab in enumerate(out):
            if lab == c:
                mine |= (wcent >= words[i][0] - HOP_S / 2) & (wcent <= words[i][1] + HOP_S / 2)
        if mine.any():
            cents[c] = normalize(x[mine].mean(0))
        elif cents[c] is None:
            cents[c] = normalize(x.mean(0))
    before, kept = {}, {}
    for i, (c, n) in enumerate(zip(wc, out)):
        d = words[i][1] - words[i][0]
        before[c] = before.get(c, 0.0) + d
        # speech that went to another voice of the same person (the wearer near- and far-field)
        # still has the same label: only a change of person, or named <-> unnamed, counts
        if n == c or (names[c] and names[n] and names[c].lower() == names[n].lower()):
            kept[c] = kept.get(c, 0.0) + d
    mixed = [c for c in before if before[c] > 0 and 0 < kept.get(c, 0.0) < scorer.mixed_keep * before[c]]
    info = {"scored": len(rows), "moved": len(target_of), "moved_s": round(moved_s, 1), "inherited": inherited,
            "moves": moves, "mixed": mixed}
    cents = np.stack(cents)
    if audio is not None and rows:
        import time
        t0 = time.process_time()
        try:   # never at the cost of the rescoring itself
            names = wearer_pass(words, units, rows, speech, out, cents, names, wscore, audio, scorer, info)
        except Exception as e:
            info["features_error"] = f"{type(e).__name__}: {str(e)[:80]}"
        info["_features_cpu_s"] = time.process_time() - t0
    return out, cents, names, info


def wearer_pass(words, units, rows, speech, out, cents, names, wscore, audio, scorer: TurnScorer, info) -> list:
    """Near-field features for each scored turn (info["turns"]) and, with a wearer model fitted
    for this scoring mode, the wearer's name by voice: each voice's wearer probability is the
    sigmoid of its turns' speech-weighted mean logit. p >= wearer_p names the voice the wearer;
    p <= 1 - wearer_p takes his name away unless the voice's own score for him reaches the
    strict threshold. Returns the names."""
    from . import wearer as wr
    feats = wr.clip_features(audio, [words[units[u][0]:units[u][1] + 1] for u in rows])
    turns = []
    for u, f in zip(rows, feats):
        if f is None:
            continue
        a, b = units[u]
        sc = wscore.get(u)
        turns.append({"start": round(words[a][0], 2), "end": round(words[b][1], 2), "speech_s": round(speech[u], 2),
                      "voice": out[a], "level_db": round(f[0], 3), "decay_db": round(f[1], 3),
                      "flatness": round(f[2], 4), "score": None if sc is None else round(sc[0], 4)})
    info["turns"] = turns
    model = scorer.wearer_model
    if model is None:
        return names
    if scorer.namer is not None and not scorer.namer.is_default:
        info["wearer"] = {"skipped": "model fitted on ECAPA window scores, naming in another space"}
        return names
    mode = getattr(scorer.calibration, "mode", "calibrated") if scorer.calibration is not None else "mean"
    if model.scoring != mode:
        info["wearer"] = {"skipped": f"model fitted for {model.scoring} scoring, running {mode}"}
        return names
    st = model.norm(scorer.device, scorer.device_stats)
    logits: dict[int, list] = {}
    for t in turns:
        if t["score"] is not None:
            z = model.logit((t["level_db"], t["decay_db"], t["flatness"]), t["score"], st)
            t["p"] = round(float(wr.sigmoid(z)), 4)
            logits.setdefault(t["voice"], []).append((z, t["speech_s"]))
    names = list(names)
    wname = scorer.wearer
    res = {"p": {}, "named": [], "unnamed": []}
    vm = scorer.matrix(cents, cents)      # each voice's own scores (its final centroid)
    vcol = {n.lower(): j for j, n in enumerate(vm.names)}
    voice_s: dict[int, float] = {}
    for i, c in enumerate(out):
        voice_s[c] = voice_s.get(c, 0.0) + words[i][1] - words[i][0]
    for c, zs in sorted(logits.items()):
        p = float(wr.sigmoid(sum(z * s for z, s in zs) / max(sum(s for _z, s in zs), 1e-9)))
        res["p"][c] = round(p, 3)
        if voice_s.get(c, 0.0) < scorer.wearer_min_s:
            continue
        is_w = bool(names[c]) and names[c].lower() == wname.lower()
        if names[c] and not is_w:
            # someone else's voice: only when that naming is weak (under its threshold + the margin)
            j = vcol.get(names[c].lower())
            if j is not None and np.isfinite(vm.match[c, j]):
                margin = scorer.margin_calibrated if vm.calibrated[c, j] else scorer.margin
                if vm.match[c, j] >= vm.threshold[c, j] + margin:
                    continue
        strict = None
        if is_w and p <= 1 - scorer.wearer_p:
            j = vcol[wname.lower()]
            strict = (float(vm.match[c, j]),
                      scorer.wearer_strict_calibrated if vm.calibrated[c, j] else scorer.wearer_strict)
        d = wr.decide(p, is_w, strict[0] if strict else None, strict[1] if strict else 0.0, scorer.wearer_p)
        if d.name:
            names[c] = wname
            res["named"].append(c)
        elif d.unname:
            names[c] = None
            res["unnamed"].append(c)
    info["wearer"] = res
    return names
