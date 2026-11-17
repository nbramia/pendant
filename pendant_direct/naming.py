"""The naming space: which model, and which audio, a voice is compared with voiceprints in.

The processor names in ECAPA windows: a voice's 1.5 s window embeddings averaged, the clustering's
own. The offline eval (`eval_speakers --spaces`, and line mode with `--set embedder=NAME
naming=window|whole`) can name in another space: `embedder` picks the model (`ecapa` or a
WeSpeaker model from embedders.py) and `naming` the audio, `window` or `whole` (a voice's speech
joined into pieces of up to 20 s, each embedded as one utterance; see speakers.Namer).
Clustering and turn boundaries always use the ECAPA windows. Voiceprints belong to one space, so
the eval rebuilds them in another from each hand-named voice's audio (`voice_audio`): its
voice-library clip, else its conversation clip cut at its stored snippet.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np

from . import embedders, speakers

DEFAULT = ("ecapa", "window")


def space(sp: dict) -> tuple[str, str]:
    """(embedder, naming) from eval settings (`embedder`, `naming`; default ECAPA windows), checked."""
    name = str(sp.get("embedder", DEFAULT[0]))
    kind = str(sp.get("naming", DEFAULT[1]))
    if name not in embedders.NAMES:
        raise ValueError(f"speakers.embedder {name!r}: one of {', '.join(embedders.NAMES)}")
    if kind not in ("window", "whole"):
        raise ValueError(f"speakers.naming {kind!r}: window or whole")
    return name, kind


def label(sp_or_space) -> str:
    name, kind = space(sp_or_space) if isinstance(sp_or_space, dict) else sp_or_space
    return f"{name}/{kind}"


def model(name: str, data: Path, ecapa=None, models: Path | None = None):
    """The embedder for `name`: ECAPA (given, else speakers.get_embedder) or a verified WeSpeaker model
    (from `models`, else <data>/models)."""
    if name == "ecapa":
        return ecapa or speakers.get_embedder(data)
    if models is not None:
        return embedders.WeSpeakerEmbedder(Path(models) / embedders.MODELS[name].file, name=name)
    return embedders.load(data, name)


def namer(sp: dict, data: Path, ecapa=None, models: Path | None = None, wrap=None):
    """speakers.Namer for the configured space; None for ECAPA windows (naming as the clustering
    sees it). `wrap(embedder, tag)` may wrap the model (the eval's cache)."""
    name, kind = space(sp)
    if (name, kind) == DEFAULT:
        return None
    emb = None
    if name != "ecapa":
        emb = model(name, data, ecapa, models)
        emb = wrap(emb, name) if wrap else emb
    elif kind == "window":
        emb = None
    return speakers.Namer(embedder=emb, whole=kind == "whole")


def library_clips(data: Path) -> dict[int, Path]:
    """{cluster id: its voice-library clip}."""
    from . import voice_library
    out = {}
    for idx in voice_library.root(data).glob("*/index.json"):
        try:
            entries = json.loads(idx.read_text())
        except (OSError, ValueError):
            continue
        for e in entries:
            src = str(e.get("src", ""))
            f = idx.parent / f"{voice_library.clip_id(src)}.opus"
            if src.startswith("cluster:") and f.exists():
                out[int(src.split(":", 1)[1])] = f
    return out


def voice_audio(con: sqlite3.Connection, data: Path, ids, lib: dict | None = None) -> dict[int, np.ndarray]:
    """{cluster id: 16 kHz audio} for the voices whose audio is still kept: the voice-library clip,
    else the conversation clip cut at the voice's stored snippet (start_s..end_s)."""
    lib = library_clips(data) if lib is None else lib
    out: dict[int, np.ndarray] = {}
    cut = []
    for cid in ids:
        if cid in lib:
            out[cid] = speakers.load_audio(lib[cid])
            continue
        row = con.execute("SELECT clip, start_s, end_s FROM speaker_clusters WHERE id = ?", (cid,)).fetchone()
        if row and row[0] and (Path(data) / "clips" / Path(row[0]).name).exists():
            cut.append((str(Path(data) / "clips" / Path(row[0]).name), float(row[1]), float(row[2]), cid))
    last, a = None, None
    for path, s, e, cid in sorted(cut):        # one conversation clip decoded at a time
        if path != last:
            last, a = path, speakers.load_audio(Path(path))
        seg = a[int(s * speakers.SAMPLE_RATE):int(e * speakers.SAMPLE_RATE)]
        if len(seg) >= speakers.SAMPLE_RATE:
            out[cid] = seg
    return out


def embed_audio(emb, audio: np.ndarray, whole: bool) -> np.ndarray:
    """A voice's unit embedding from its audio: one utterance (whole), or the mean of 1.5 s windows
    every 0.75 s (window)."""
    if whole:
        return speakers.normalize(np.asarray(emb.embed_wavs([audio]), dtype=np.float32)[0])
    dur = len(audio) / speakers.SAMPLE_RATE
    starts = np.arange(0.0, max(dur - speakers.WINDOW_S, 0.0) + 1e-9, speakers.HOP_S)
    spans = [(float(t), float(min(t + speakers.WINDOW_S, dur))) for t in starts]
    e = speakers.normalize(np.asarray(emb.embed_audio(audio, spans), dtype=np.float32))
    return speakers.normalize(e.mean(0))


WHOLE_MAX_PIECES = 30        # a kept voice: at most this many 20 s pieces (10 min of speech)...
WHOLE_MAX_WINDOWS = 2400     # ...or 1.5 s windows, spread over all of it


def kept_pieces(file_segments, piece_s: float = speakers.PIECE_S, max_pieces: int = WHOLE_MAX_PIECES) -> list:
    """A kept voice's lines (spans in its file) as pieces to embed whole: [[(start, end), ...]] of at most
    piece_s each, filled in time order (a longer line is split); the max_pieces with the most speech."""
    pieces: list[list[tuple]] = [[]]
    room = piece_s
    for a, b in file_segments:
        a, b = float(a), float(b)
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


def kept_windows(file_segments, max_windows: int = WHOLE_MAX_WINDOWS) -> list[tuple[float, float]]:
    """1.5 s windows every 0.75 s inside each line (a shorter line is one window), evenly thinned to
    max_windows."""
    spans = []
    for a, b in file_segments:
        a, b = float(a), float(b)
        if b - a < speakers.WINDOW_S:
            if b - a >= 0.5:
                spans.append((a, b))
            continue
        for t in np.arange(a, b - speakers.WINDOW_S + 1e-9, speakers.HOP_S):
            spans.append((float(t), float(t + speakers.WINDOW_S)))
    if len(spans) > max_windows:
        idx = np.linspace(0, len(spans) - 1, max_windows).round().astype(int)
        spans = [spans[i] for i in idx]
    return spans


def embed_kept(emb, audio: np.ndarray, file_segments, whole: bool) -> np.ndarray | None:
    """A kept voice's unit embedding: the unit mean of its pieces embedded whole, or of its windows."""
    if whole:
        pieces = kept_pieces(file_segments)
        if not pieces:
            return None
        sr = speakers.SAMPLE_RATE
        wavs = [np.concatenate([audio[int(a * sr):int(b * sr)] for a, b in p]) for p in pieces]
        e = speakers.normalize(np.asarray(emb.embed_wavs(wavs), dtype=np.float32))
        return speakers.normalize(e.mean(0))
    spans = kept_windows(file_segments)
    if not spans:
        return None
    e = speakers.normalize(np.asarray(emb.embed_audio(audio, spans), dtype=np.float32))
    return speakers.normalize(e.mean(0))
