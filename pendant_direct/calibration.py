"""Calibrated voice scores: top-k sample cosine plus adaptive s-norm.

A voice is scored against each person's individual samples (the mean of its
`k = min(3, samples)` closest cosines), not against one average, so a person who
sounds different in the car and in the kitchen is still recognised. The raw score
is then normalised against a cohort of other voices (strangers, other people's
samples, unlabeled clusters of other conversations):

    norm = 0.5 * ((raw - mu_voice) / sd_voice + (raw - mu_person) / sd_person)

where mu/sd are the mean and standard deviation of the top-N cohort cosines of the
scored voice and of the person's samples (adaptive s-norm). A person's own
labelled voices are left out of their cohort by identity (never by similarity, which
would drop the hard negatives s-norm needs); unlabeled and stranger voices stay even
when they sound alike. A voice whose raw score is under MIN_RAW
is capped at 0 (s-norm lifts voices that sit far from everything). The cohort never holds
the voice being scored (removed by cluster id, never by similarity) or anything from its conversation, and the person being
scored never appears in their own cohort. Fewer than MIN_COHORT cohort voices
(counted per person, once their own entries are left out) fall back to the raw top-k cosine
and the old threshold: `Result.calibrated` says which scale each (voice, person) score is on and
`Result.threshold` the cutoff that goes with it.

numpy is imported at module level: import this lazily from process.py.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .config import DEFAULTS

TOP_K = 3
TOP_N = 10
MIN_COHORT = 5
SD_FLOOR = 0.01
STRANGER = "#stranger"          # cohort owner of a "random person" voice
MIN_RAW = 0.4                   # below this top-k cosine a voice doesn't resemble the person at all: score capped at 0

Key = tuple                     # (device, start_ms) of a conversation


def _unit(x) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    return x / np.maximum(np.linalg.norm(x, axis=-1, keepdims=True), 1e-9)


@dataclass
class Cohort:
    embs: np.ndarray                       # (M, dim) unit embeddings
    keys: list[Key | None]                 # conversation of each entry (None: unknown)
    owners: list[str | None]               # person the entry is a sample of (None: unlabeled)
    ids: list | None = None                # cluster id of each entry (None: not tracked)

    @classmethod
    def empty(cls, dim: int = 0) -> Cohort:
        return cls(np.zeros((0, dim), np.float32), [], [])

    def __len__(self) -> int:
        return len(self.keys)

    def without(self, key: Key | None) -> Cohort:
        """The cohort minus every entry from conversation `key`."""
        if key is None or not len(self):
            return self
        keep = [i for i, k in enumerate(self.keys) if k != key]
        return Cohort(self.embs[keep], [self.keys[i] for i in keep], [self.owners[i] for i in keep],
                      None if self.ids is None else [self.ids[i] for i in keep])

    def mask(self, exclude: Key | None, drop: set) -> np.ndarray:
        """Entries that may be compared against a voice: not from conversation `exclude`,
        and not the voice itself, which is dropped by cluster id (`drop`), never by
        similarity: another recording that merely sounds identical stays."""
        keep = np.ones(len(self), dtype=bool)
        if exclude is not None:
            keep &= np.array([k != exclude for k in self.keys], dtype=bool)
        if drop and self.ids is not None:
            keep &= np.array([c is None or c not in drop for c in self.ids], dtype=bool)
        return keep


@dataclass
class Result:
    raw: np.ndarray             # (n, P) top-k cosine; -inf where a person has no sample
    match: np.ndarray           # (n, P) what to compare with `threshold`: s-norm, or raw on fallback
    calibrated: np.ndarray      # (n, P) bool: this person's score is on the calibrated scale
    threshold: np.ndarray       # (n, P) cutoff for that scale


@dataclass
class Calibration:
    samples: dict[str, tuple[np.ndarray, list]]    # name -> (unit embeddings, cluster ids; None = legacy)
    cohort: Cohort
    threshold: float = 1.0                   # speakers.match_threshold_calibrated
    raw_threshold: float = 0.5               # speakers.match_threshold (the fallback)
    k: int = TOP_K
    top_n: int = TOP_N
    min_cohort: int = MIN_COHORT
    mode: str = "calibrated"                 # or "topk": raw top-k scores, no cohort
    names: list[str] = field(init=False)
    _cache: dict = field(init=False, default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.names = [n for n in self.samples if len(self.samples[n][0])]
        self._cache = {}

    # -- pieces ------------------------------------------------------------

    def _target(self, name: str, smask: np.ndarray, cmask: np.ndarray, key: bytes) -> tuple[float, float]:
        ck = (name, smask.tobytes(), key)
        if ck not in self._cache:
            embs = self.samples[name][0][smask]
            sims = embs @ self.cohort.embs[cmask].T
            self._cache[ck] = _top_stats(sims, self.top_n)
        return self._cache[ck]

    def scores(self, x, exclude=None, self_ids=None) -> Result:
        """Score each row of x against every person. `exclude`: a conversation key
        (all rows) or one per row, whose cohort entries are left out; `self_ids`:
        per row, a cluster id (or list of ids) whose sample must not count for
        anyone (leave-one-out, and a voice never matches its own enrolment)."""
        x = _unit(x)
        n, names, P = len(x), self.names, len(self.names)
        raw = np.full((n, P), -np.inf, np.float32)
        match = np.full((n, P), -np.inf, np.float32)
        calibrated = np.zeros((n, P), dtype=bool)
        if isinstance(exclude, tuple) or exclude is None:
            exclude = [exclude] * n
        self_ids = list(self_ids) if self_ids is not None else [None] * n
        C = self.cohort
        csim = x @ C.embs.T if len(C) else np.zeros((n, 0), np.float32)
        owners = np.array([o or "" for o in C.owners], dtype=object)
        for i in range(n):
            drop = _as_set(self_ids[i])
            cmask = C.mask(exclude[i], drop)
            for j, name in enumerate(names):
                embs, ids = self.samples[name]
                smask = np.array([c is None or c not in drop for c in ids], dtype=bool)
                if not smask.any():
                    continue
                sims = embs[smask] @ x[i]
                raw[i, j] = float(np.sort(sims)[::-1][:min(self.k, len(sims))].mean())
                match[i, j] = raw[i, j]
                if self.mode == "topk":
                    continue
                # the person's own voices are never their impostors (by label, not similarity)
                pmask = cmask & (owners != name)
                if int(pmask.sum()) < self.min_cohort:
                    continue                     # this person's cohort is too small: raw score, old threshold
                mu_e, sd_e = _top_stats(csim[i][pmask][None, :], self.top_n)
                mu_t, sd_t = self._target(name, smask, pmask, pmask.tobytes())
                z = 0.5 * ((raw[i, j] - mu_e) / sd_e + (raw[i, j] - mu_t) / sd_t)
                match[i, j] = min(z, 0.0) if raw[i, j] < MIN_RAW else z
                calibrated[i, j] = True
        thr = np.where(calibrated, self.threshold, self.raw_threshold)
        return Result(raw, match, calibrated, thr)


def _as_set(v) -> set:
    if v is None:
        return set()
    if isinstance(v, (set, list, tuple)):
        return set(v)
    return {v}


def _top_stats(sims: np.ndarray, top_n: int) -> tuple[float, float]:
    """Mean and sd (floored) of the top_n entries of each row, pooled."""
    n = min(top_n, sims.shape[1])
    top = -np.sort(-sims, axis=1)[:, :n]
    return float(top.mean()), max(float(top.std()), SD_FLOOR)


# ---------------------------------------------------------------------------
# loading from the database and voiceprint files

def conversation_keys(con: sqlite3.Connection) -> dict[int, Key]:
    try:
        return {r[0]: (r[1], r[2]) for r in con.execute("SELECT id, device, start_ms FROM speaker_clusters")}
    except sqlite3.OperationalError:
        return {}


def build_cohort(con: sqlite3.Connection, samples: dict[str, tuple[np.ndarray, list]]) -> Cohort:
    """Cohort entries, one per cluster id at most: other people's samples (every
    sample in `samples`), stranger embeddings, and unlabeled clusters (no label,
    not noise). Entries from different dimensions are dropped."""
    keys_of = conversation_keys(con)
    embs: list[np.ndarray] = []
    keys: list[Key | None] = []
    owners: list[str | None] = []
    cids: list[int | None] = []
    seen: set[int] = set()
    dim = next((e.shape[1] for e, _ in samples.values() if len(e)), None)

    def add(emb, key, owner, cid):
        nonlocal dim
        v = np.frombuffer(emb, dtype=np.float32) if isinstance(emb, (bytes, bytearray, memoryview)) else emb
        if dim is None:
            dim = len(v)
        if len(v) != dim or (cid is not None and cid in seen):
            return
        if cid is not None:
            seen.add(cid)
        embs.append(v)
        keys.append(key)
        owners.append(owner)
        cids.append(cid)

    for name, (e, ids) in samples.items():
        for row, cid in zip(e, ids):
            add(row, keys_of.get(cid) if cid is not None else None, name, cid)
    try:
        for cid, emb in con.execute("SELECT cluster_id, embedding FROM strangers WHERE embedding IS NOT NULL"):
            add(emb, keys_of.get(cid), STRANGER, cid)
        for cid, dev, ms, emb in con.execute(
                "SELECT id, device, start_ms, embedding FROM speaker_clusters WHERE label IS NULL "
                "AND COALESCE(noise, 0) = 0 AND COALESCE(labeled_by, '') != 'dismissed' "
                "AND COALESCE(label_kind, '') NOT IN ('stranger', 'noise') AND embedding IS NOT NULL"):
            add(emb, (dev, ms), None, cid)
    except sqlite3.OperationalError:
        pass
    if not embs:
        return Cohort.empty(dim or 0)
    return Cohort(_unit(np.stack(embs)), keys, owners, cids)


def load(con: sqlite3.Connection, vdir: Path, cfg: dict) -> Calibration | None:
    """The scorer for `speakers.scoring`: None for "mean" (the mean voiceprints and
    `match_threshold`, as before per-sample scoring) or when nobody is enrolled;
    otherwise every enrolled person's samples, with a cohort from the database in
    "calibrated" mode."""
    from . import speakers
    from .config import scoring_mode
    mode = scoring_mode(cfg)
    if mode == "mean":
        return None
    samples = speakers.load_samples(vdir)
    if not samples:
        return None
    dim = next(len(e[0]) for e, _ in samples.values() if len(e))
    return Calibration(samples, build_cohort(con, samples) if mode == "calibrated" else Cohort.empty(dim),
                       mode=mode,
                       threshold=float(cfg.get("match_threshold_calibrated", DEFAULTS["speakers"]["match_threshold_calibrated"])),
                       raw_threshold=float(cfg.get("match_threshold", 0.5)))
